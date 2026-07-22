"""Postgres persistence for the Maitre D, via this server's shared
SessionLocal/ORM (app.core.db) -- replaces the maitre-d-agent branch's
single-tenant SQLite Store. Every query is scoped to one store_id, bound at
construction, so MaitreD's own call sites (agent.py) barely changed from
the original branch: `self.store.get_guest(phone)` etc. still reads as
single-venue code, it's just scoped under the hood.

Lookups by a public id (reservation_id / waitlist_id) still verify the row
belongs to this store_id before returning it, even though those ids are
globally unique -- a staff member typing a stray id from another store's
booking must not be able to see or act on it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app.agents.maitre_d.models import Guest, Reservation, WaitlistEntry

# Statuses that still occupy a table (so they count against capacity).
ACTIVE_RESERVATION_STATUSES = ("pending", "confirmed", "seated")


def _new_uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


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

    # ── reservations ──

    def add_reservation(self, res: Reservation) -> Reservation:
        from app.core.db import SessionLocal, MaitreDReservation
        if not res.reservation_id:
            res.reservation_id = _new_uid("res")
        with SessionLocal() as db:
            db.add(MaitreDReservation(
                reservation_uid=res.reservation_id, store_id=self.store_id,
                phone=res.phone, name=res.name, party_size=res.party_size,
                when_at=res.when, status=res.status, table_id=res.table_id,
                is_vip=res.is_vip, vip_tier=res.vip_tier,
                no_show_risk=res.no_show_risk, no_show_band=res.no_show_band,
                deposit_required=res.deposit_required, deposit_paid=res.deposit_paid,
                payment_ref=res.payment_ref, reminder_sent=res.reminder_sent,
                special_requests=res.special_requests, source=res.source,
                created_at=res.created_at,
            ))
            db.commit()
        res.store_id = self.store_id
        return res

    def update_reservation_status(self, reservation_id: str, status: str) -> None:
        from app.core.db import SessionLocal, MaitreDReservation
        with SessionLocal() as db:
            row = db.query(MaitreDReservation).filter(
                MaitreDReservation.store_id == self.store_id,
                MaitreDReservation.reservation_uid == reservation_id,
            ).first()
            if row:
                row.status = status
                db.commit()

    def set_payment_ref(self, reservation_id: str, payment_ref: str) -> None:
        from app.core.db import SessionLocal, MaitreDReservation
        with SessionLocal() as db:
            row = db.query(MaitreDReservation).filter(
                MaitreDReservation.store_id == self.store_id,
                MaitreDReservation.reservation_uid == reservation_id,
            ).first()
            if row:
                row.payment_ref = payment_ref
                db.commit()

    def mark_deposit_paid(self, reservation_id: str) -> None:
        from app.core.db import SessionLocal, MaitreDReservation
        with SessionLocal() as db:
            row = db.query(MaitreDReservation).filter(
                MaitreDReservation.store_id == self.store_id,
                MaitreDReservation.reservation_uid == reservation_id,
            ).first()
            if row:
                row.deposit_paid = True
                db.commit()

    def mark_reminder_sent(self, reservation_id: str) -> None:
        from app.core.db import SessionLocal, MaitreDReservation
        with SessionLocal() as db:
            row = db.query(MaitreDReservation).filter(
                MaitreDReservation.store_id == self.store_id,
                MaitreDReservation.reservation_uid == reservation_id,
            ).first()
            if row:
                row.reminder_sent = True
                db.commit()

    def get_by_payment_ref(self, payment_ref: str) -> Reservation | None:
        from app.core.db import SessionLocal, MaitreDReservation
        if not payment_ref:
            return None
        with SessionLocal() as db:
            row = db.query(MaitreDReservation).filter(
                MaitreDReservation.store_id == self.store_id,
                MaitreDReservation.payment_ref == payment_ref,
            ).first()
            return self._row_to_reservation(row) if row else None

    def reservations_due(
        self, statuses: tuple[str, ...], when_le: datetime
    ) -> list[Reservation]:
        """Reservations in `statuses` whose start time is at or before `when_le`."""
        from app.core.db import SessionLocal, MaitreDReservation
        with SessionLocal() as db:
            rows = db.query(MaitreDReservation).filter(
                MaitreDReservation.store_id == self.store_id,
                MaitreDReservation.status.in_(statuses),
                MaitreDReservation.when_at <= when_le,
            ).order_by(MaitreDReservation.when_at).all()
            return [self._row_to_reservation(r) for r in rows]

    def reminders_due(self, now: datetime, lead_hours: int) -> list[Reservation]:
        """Confirmed, unreminded bookings starting within `lead_hours` of now."""
        from app.core.db import SessionLocal, MaitreDReservation
        horizon = now + timedelta(hours=lead_hours)
        with SessionLocal() as db:
            rows = db.query(MaitreDReservation).filter(
                MaitreDReservation.store_id == self.store_id,
                MaitreDReservation.status == "confirmed",
                MaitreDReservation.reminder_sent.is_(False),
                MaitreDReservation.when_at > now,
                MaitreDReservation.when_at <= horizon,
            ).order_by(MaitreDReservation.when_at).all()
            return [self._row_to_reservation(r) for r in rows]

    def get_reservation(self, reservation_id: str) -> Reservation | None:
        from app.core.db import SessionLocal, MaitreDReservation
        with SessionLocal() as db:
            row = db.query(MaitreDReservation).filter(
                MaitreDReservation.store_id == self.store_id,
                MaitreDReservation.reservation_uid == reservation_id,
            ).first()
            return self._row_to_reservation(row) if row else None

    def latest_active_reservation_for(self, phone: str) -> Reservation | None:
        from app.core.db import SessionLocal, MaitreDReservation
        with SessionLocal() as db:
            row = db.query(MaitreDReservation).filter(
                MaitreDReservation.store_id == self.store_id,
                MaitreDReservation.phone == phone,
                MaitreDReservation.status.in_(ACTIVE_RESERVATION_STATUSES),
            ).order_by(MaitreDReservation.when_at.asc()).first()
            return self._row_to_reservation(row) if row else None

    def reservation_history_for(self, phone: str) -> list[Reservation]:
        from app.core.db import SessionLocal, MaitreDReservation
        with SessionLocal() as db:
            rows = db.query(MaitreDReservation).filter(
                MaitreDReservation.store_id == self.store_id,
                MaitreDReservation.phone == phone,
            ).order_by(MaitreDReservation.when_at).all()
            return [self._row_to_reservation(r) for r in rows]

    def active_reservations_overlapping(
        self, when: datetime, turn_minutes: int
    ) -> list[Reservation]:
        """Active reservations whose hold window overlaps [when, when+turn).

        Two intervals overlap iff each starts before the other ends. We
        over-fetch a day around the target and filter in Python to keep the
        query simple, matching the original SQLite implementation."""
        from app.core.db import SessionLocal, MaitreDReservation
        new_start = when
        new_end = when + timedelta(minutes=turn_minutes)
        lo = when - timedelta(hours=12)
        hi = when + timedelta(hours=12)
        with SessionLocal() as db:
            rows = db.query(MaitreDReservation).filter(
                MaitreDReservation.store_id == self.store_id,
                MaitreDReservation.status.in_(ACTIVE_RESERVATION_STATUSES),
                MaitreDReservation.when_at >= lo,
                MaitreDReservation.when_at <= hi,
            ).all()
            out = []
            for row in rows:
                res = self._row_to_reservation(row)
                existing_start = res.when
                existing_end = res.when + timedelta(minutes=turn_minutes)
                if new_start < existing_end and existing_start < new_end:
                    out.append(res)
            return out

    def list_reservations(self, status: str | None = None) -> list[Reservation]:
        from app.core.db import SessionLocal, MaitreDReservation
        with SessionLocal() as db:
            q = db.query(MaitreDReservation).filter(MaitreDReservation.store_id == self.store_id)
            if status:
                q = q.filter(MaitreDReservation.status == status)
            rows = q.order_by(MaitreDReservation.when_at).all()
            return [self._row_to_reservation(r) for r in rows]

    def _row_to_reservation(self, row) -> Reservation:
        return Reservation(
            reservation_id=row.reservation_uid, store_id=row.store_id,
            phone=row.phone, name=row.name, party_size=row.party_size,
            when=row.when_at, status=row.status, table_id=row.table_id,
            is_vip=row.is_vip, vip_tier=row.vip_tier,
            no_show_risk=row.no_show_risk, no_show_band=row.no_show_band,
            deposit_required=row.deposit_required, deposit_paid=row.deposit_paid,
            payment_ref=row.payment_ref or "", reminder_sent=row.reminder_sent,
            special_requests=row.special_requests, source=row.source,
            created_at=row.created_at,
        )

    # ── waitlist ──

    def add_waitlist(self, entry: WaitlistEntry) -> WaitlistEntry:
        from app.core.db import SessionLocal, MaitreDWaitlist
        if not entry.waitlist_id:
            entry.waitlist_id = _new_uid("wl")
        with SessionLocal() as db:
            db.add(MaitreDWaitlist(
                waitlist_uid=entry.waitlist_id, store_id=self.store_id,
                phone=entry.phone, name=entry.name, party_size=entry.party_size,
                requested_when=entry.requested_when, status=entry.status,
                is_vip=entry.is_vip, vip_tier=entry.vip_tier,
                offered_at=entry.offered_at, created_at=entry.created_at,
            ))
            db.commit()
        entry.store_id = self.store_id
        return entry

    def update_waitlist_status(
        self, waitlist_id: str, status: str, offered_at: datetime | None = None
    ) -> None:
        from app.core.db import SessionLocal, MaitreDWaitlist
        with SessionLocal() as db:
            row = db.query(MaitreDWaitlist).filter(
                MaitreDWaitlist.store_id == self.store_id,
                MaitreDWaitlist.waitlist_uid == waitlist_id,
            ).first()
            if row:
                row.status = status
                if offered_at is not None:
                    row.offered_at = offered_at
                db.commit()

    def waiting_entries_near(
        self, when: datetime, window_hours: int = 2
    ) -> list[WaitlistEntry]:
        """People still waiting whose requested time is near `when`.
        VIPs first, then earliest-requested first (fair queue)."""
        from app.core.db import SessionLocal, MaitreDWaitlist
        lo = when - timedelta(hours=window_hours)
        hi = when + timedelta(hours=window_hours)
        with SessionLocal() as db:
            rows = db.query(MaitreDWaitlist).filter(
                MaitreDWaitlist.store_id == self.store_id,
                MaitreDWaitlist.status == "waiting",
                MaitreDWaitlist.requested_when >= lo,
                MaitreDWaitlist.requested_when <= hi,
            ).order_by(MaitreDWaitlist.is_vip.desc(), MaitreDWaitlist.created_at.asc()).all()
            return [self._row_to_waitlist(r) for r in rows]

    def offered_entries_before(self, offered_le: datetime) -> list[WaitlistEntry]:
        """Offers made at or before `offered_le` that are still unanswered."""
        from app.core.db import SessionLocal, MaitreDWaitlist
        with SessionLocal() as db:
            rows = db.query(MaitreDWaitlist).filter(
                MaitreDWaitlist.store_id == self.store_id,
                MaitreDWaitlist.status == "offered",
                MaitreDWaitlist.offered_at.isnot(None),
                MaitreDWaitlist.offered_at <= offered_le,
            ).order_by(MaitreDWaitlist.offered_at).all()
            return [self._row_to_waitlist(r) for r in rows]

    def list_waitlist(self, status: str | None = None) -> list[WaitlistEntry]:
        from app.core.db import SessionLocal, MaitreDWaitlist
        with SessionLocal() as db:
            q = db.query(MaitreDWaitlist).filter(MaitreDWaitlist.store_id == self.store_id)
            if status:
                q = q.filter(MaitreDWaitlist.status == status)
            rows = q.order_by(MaitreDWaitlist.created_at).all()
            return [self._row_to_waitlist(r) for r in rows]

    def _row_to_waitlist(self, row) -> WaitlistEntry:
        return WaitlistEntry(
            waitlist_id=row.waitlist_uid, store_id=row.store_id, phone=row.phone,
            name=row.name, party_size=row.party_size,
            requested_when=row.requested_when, status=row.status,
            is_vip=row.is_vip, vip_tier=row.vip_tier, offered_at=row.offered_at,
            created_at=row.created_at,
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
