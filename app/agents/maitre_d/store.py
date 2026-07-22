"""Postgres persistence for the Maitre D, via this server's shared
SessionLocal/ORM (app.core.db) -- replaces the maitre-d-agent branch's
single-tenant SQLite Store. Every query is scoped to one store_id, bound at
construction, so MaitreD's own call sites (agent.py) barely changed from
the original branch: `self.store.get_guest(phone)` etc. still reads as
single-venue code, it's just scoped under the hood.

Lookups by a public id still verify the row belongs to this store_id
before returning it, even though ids are globally unique -- a staff member
typing a stray id from another store's queue must not be able to see or
act on it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.agents.maitre_d.models import Guest, QueueEntry


class Store:
    def __init__(self, store_id: int) -> None:
        self.store_id = store_id

    # ── guests ──

    def upsert_guest(self, guest: Guest) -> None:
        from app.core.db import SessionLocal, MaitreDGuest
        with SessionLocal() as db:
            row = db.query(MaitreDGuest).filter(
                MaitreDGuest.store_id == self.store_id,
                MaitreDGuest.phone == guest.phone,
            ).first()
            if row:
                if guest.name:
                    row.name = guest.name
                if guest.vip_tier:
                    row.vip_tier = guest.vip_tier
                if guest.vip_notes:
                    row.vip_notes = guest.vip_notes
            else:
                db.add(MaitreDGuest(
                    store_id=self.store_id, phone=guest.phone, name=guest.name,
                    vip_tier=guest.vip_tier, vip_notes=guest.vip_notes,
                    created_at=guest.created_at,
                ))
            db.commit()

    def get_guest(self, phone: str) -> Guest | None:
        from app.core.db import SessionLocal, MaitreDGuest
        with SessionLocal() as db:
            row = db.query(MaitreDGuest).filter(
                MaitreDGuest.store_id == self.store_id, MaitreDGuest.phone == phone,
            ).first()
            if not row:
                return None
            return Guest(
                store_id=self.store_id, phone=row.phone, name=row.name,
                vip_tier=row.vip_tier, vip_notes=row.vip_notes,
                created_at=row.created_at,
            )

    # ── live queue ──

    def add_queue_entry(self, entry: QueueEntry, day_start: datetime) -> QueueEntry:
        """Appends `entry` to the end of its location's live queue.
        queue_number is the next unused number for this store+location
        since `day_start` (the venue-local start of today); position is
        the next unused slot among that location's current "waiting" rows.
        """
        from sqlalchemy import func
        from app.core.db import SessionLocal, MaitreDQueueEntry
        with SessionLocal() as db:
            loc_id = entry.location_id or None
            queue_number = (db.query(func.max(MaitreDQueueEntry.queue_number)).filter(
                MaitreDQueueEntry.store_id == self.store_id,
                MaitreDQueueEntry.location_id == loc_id,
                MaitreDQueueEntry.created_at >= day_start,
            ).scalar() or 0) + 1
            position = (db.query(func.max(MaitreDQueueEntry.position)).filter(
                MaitreDQueueEntry.store_id == self.store_id,
                MaitreDQueueEntry.location_id == loc_id,
                MaitreDQueueEntry.status == "waiting",
            ).scalar() or 0) + 1
            row = MaitreDQueueEntry(
                store_id=self.store_id, location_id=loc_id, branch_name=entry.branch_name,
                queue_number=queue_number, phone=entry.phone, name=entry.name,
                party_size=entry.party_size, special_requests=entry.special_requests,
                status="waiting", position=position, is_vip=entry.is_vip,
                vip_tier=entry.vip_tier, created_at=entry.created_at,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return self._row_to_queue_entry(row)

    def insert_at_position(
        self, entry: QueueEntry, position: int, day_start: datetime,
    ) -> tuple[QueueEntry, list[QueueEntry]]:
        """Staff-driven insert: pushes everyone already at `position` or
        later back by one, then places `entry` there. `position` is
        clamped into [1, current_count + 1] so an out-of-range number
        (e.g. "add 99 ...") just appends to the end instead of leaving a
        gap or erroring. Returns (new_entry, pushed_back) -- `pushed_back`
        is everyone whose position moved, for the caller to notify."""
        from sqlalchemy import func
        from app.core.db import SessionLocal, MaitreDQueueEntry
        with SessionLocal() as db:
            loc_id = entry.location_id or None
            queue_number = (db.query(func.max(MaitreDQueueEntry.queue_number)).filter(
                MaitreDQueueEntry.store_id == self.store_id,
                MaitreDQueueEntry.location_id == loc_id,
                MaitreDQueueEntry.created_at >= day_start,
            ).scalar() or 0) + 1

            waiting = db.query(MaitreDQueueEntry).filter(
                MaitreDQueueEntry.store_id == self.store_id,
                MaitreDQueueEntry.location_id == loc_id,
                MaitreDQueueEntry.status == "waiting",
            ).all()
            position = max(1, min(position, len(waiting) + 1))
            pushed_back = [row for row in waiting if row.position >= position]
            for row in pushed_back:
                row.position += 1

            new_row = MaitreDQueueEntry(
                store_id=self.store_id, location_id=loc_id, branch_name=entry.branch_name,
                queue_number=queue_number, phone=entry.phone, name=entry.name,
                party_size=entry.party_size, special_requests=entry.special_requests,
                status="waiting", position=position, is_vip=entry.is_vip,
                vip_tier=entry.vip_tier, created_at=entry.created_at,
            )
            db.add(new_row)
            db.commit()
            db.refresh(new_row)
            for row in pushed_back:
                db.refresh(row)
            return self._row_to_queue_entry(new_row), [self._row_to_queue_entry(r) for r in pushed_back]

    def _close_gap(self, db, row) -> list:
        """After `row` (already updated in-session, still holding its old
        position) leaves the "waiting" set, shifts everyone behind it at
        the same location down by one so positions stay dense from 1.
        Returns the (still session-attached) ORM rows that were shifted,
        for the caller to notify once committed."""
        from app.core.db import MaitreDQueueEntry
        behind = db.query(MaitreDQueueEntry).filter(
            MaitreDQueueEntry.store_id == self.store_id,
            MaitreDQueueEntry.location_id == row.location_id,
            MaitreDQueueEntry.status == "waiting",
            MaitreDQueueEntry.position > row.position,
        ).all()
        for r in behind:
            r.position -= 1
        return behind

    def admit_next(self, location_id: int | None) -> tuple[QueueEntry | None, list[QueueEntry]]:
        """Pops position 1 (the guest who's been waiting longest at the
        front) -- the "restaurant has given them seating" action. Returns
        (admitted_entry_or_None, moved_up) -- `moved_up` is everyone whose
        position advanced, for the caller to notify."""
        from app.core.db import SessionLocal, MaitreDQueueEntry
        with SessionLocal() as db:
            row = db.query(MaitreDQueueEntry).filter(
                MaitreDQueueEntry.store_id == self.store_id,
                MaitreDQueueEntry.location_id == (location_id or None),
                MaitreDQueueEntry.status == "waiting",
            ).order_by(MaitreDQueueEntry.position).first()
            if not row:
                return None, []
            moved_up = self._close_gap(db, row)
            row.status = "admitted"
            row.admitted_at = datetime.utcnow()
            db.commit()
            db.refresh(row)
            for r in moved_up:
                db.refresh(r)
            return self._row_to_queue_entry(row), [self._row_to_queue_entry(r) for r in moved_up]

    def remove_at_position(
        self, location_id: int | None, position: int,
    ) -> tuple[QueueEntry | None, list[QueueEntry]]:
        from app.core.db import SessionLocal, MaitreDQueueEntry
        with SessionLocal() as db:
            row = db.query(MaitreDQueueEntry).filter(
                MaitreDQueueEntry.store_id == self.store_id,
                MaitreDQueueEntry.location_id == (location_id or None),
                MaitreDQueueEntry.status == "waiting",
                MaitreDQueueEntry.position == position,
            ).first()
            if not row:
                return None, []
            moved_up = self._close_gap(db, row)
            row.status = "removed"
            db.commit()
            db.refresh(row)
            for r in moved_up:
                db.refresh(r)
            return self._row_to_queue_entry(row), [self._row_to_queue_entry(r) for r in moved_up]

    def remove_by_id(
        self, entry_id: int, new_status: str = "cancelled",
    ) -> tuple[QueueEntry | None, list[QueueEntry]]:
        """Guest-initiated leave ("cancel") -- looked up by row id, not
        position, since the guest doesn't know or care about their numeric
        slot."""
        from app.core.db import SessionLocal, MaitreDQueueEntry
        with SessionLocal() as db:
            row = db.query(MaitreDQueueEntry).filter(
                MaitreDQueueEntry.store_id == self.store_id,
                MaitreDQueueEntry.id == entry_id,
                MaitreDQueueEntry.status == "waiting",
            ).first()
            if not row:
                return None, []
            moved_up = self._close_gap(db, row)
            row.status = new_status
            db.commit()
            db.refresh(row)
            for r in moved_up:
                db.refresh(r)
            return self._row_to_queue_entry(row), [self._row_to_queue_entry(r) for r in moved_up]

    def latest_waiting_entry_for(self, phone: str) -> QueueEntry | None:
        from app.core.db import SessionLocal, MaitreDQueueEntry
        with SessionLocal() as db:
            row = db.query(MaitreDQueueEntry).filter(
                MaitreDQueueEntry.store_id == self.store_id,
                MaitreDQueueEntry.phone == phone,
                MaitreDQueueEntry.status == "waiting",
            ).order_by(MaitreDQueueEntry.created_at.desc()).first()
            return self._row_to_queue_entry(row) if row else None

    def list_queue(self, location_id: int | None = None, status: str = "waiting") -> list[QueueEntry]:
        from app.core.db import SessionLocal, MaitreDQueueEntry
        with SessionLocal() as db:
            q = db.query(MaitreDQueueEntry).filter(MaitreDQueueEntry.store_id == self.store_id)
            if status:
                q = q.filter(MaitreDQueueEntry.status == status)
            if location_id:
                q = q.filter(MaitreDQueueEntry.location_id == location_id)
            rows = q.order_by(MaitreDQueueEntry.location_id, MaitreDQueueEntry.position).all()
            return [self._row_to_queue_entry(r) for r in rows]

    def _row_to_queue_entry(self, row) -> QueueEntry:
        return QueueEntry(
            id=row.id, store_id=row.store_id, location_id=row.location_id or 0,
            branch_name=row.branch_name or "", queue_number=row.queue_number,
            phone=row.phone, name=row.name, party_size=row.party_size,
            special_requests=row.special_requests or "", status=row.status,
            position=row.position, is_vip=row.is_vip, vip_tier=row.vip_tier,
            created_at=row.created_at, admitted_at=row.admitted_at,
        )

    # ── conversation state (slot-filling across messages) ──

    def get_conversation(
        self, phone: str, ttl_minutes: int | None = None,
        now: datetime | None = None,
    ) -> dict:
        from app.core.db import SessionLocal, MaitreDConversation
        with SessionLocal() as db:
            row = db.query(MaitreDConversation).filter(
                MaitreDConversation.store_id == self.store_id,
                MaitreDConversation.phone == phone,
            ).first()
            if not row:
                return {}
            if ttl_minutes:
                ref = now or datetime.now()
                if row.updated_at and (ref - row.updated_at) > timedelta(minutes=ttl_minutes):
                    db.delete(row)
                    db.commit()
                    return {}  # abandoned flow — forget it
            return dict(row.state or {})

    def set_conversation(
        self, phone: str, state: dict, now: datetime | None = None
    ) -> None:
        from app.core.db import SessionLocal, MaitreDConversation
        with SessionLocal() as db:
            row = db.query(MaitreDConversation).filter(
                MaitreDConversation.store_id == self.store_id,
                MaitreDConversation.phone == phone,
            ).first()
            ts = now or datetime.now()
            if row:
                row.state = state
                row.updated_at = ts
            else:
                db.add(MaitreDConversation(
                    store_id=self.store_id, phone=phone, state=state, updated_at=ts,
                ))
            db.commit()

    def clear_conversation(self, phone: str) -> None:
        from app.core.db import SessionLocal, MaitreDConversation
        with SessionLocal() as db:
            db.query(MaitreDConversation).filter(
                MaitreDConversation.store_id == self.store_id,
                MaitreDConversation.phone == phone,
            ).delete()
            db.commit()
