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

Every method that mutates the live queue (add_queue_entry, insert_at_
position, admit_next, remove_at_position, remove_by_id) first locks that
location's MaitreDQueueCounter row (SELECT ... FOR UPDATE, creating it if
needed) -- see that model's docstring in app.core.db. Without this, two
concurrent requests (two guests scanning the entrance QR in the same
instant, or a guest joining while staff run "admit") could both read the
same "current max position"/"current max queue_number" before either
commits, handing out duplicates. Confirmed as a real gap, not
theoretical: WEB_CONCURRENCY=4 means this server genuinely runs requests
in parallel.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

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

    # ── live queue: concurrency-safety anchor ──

    def _lock_location_counter(self, db, loc_id: int | None, today: date | None = None):
        """Locks (creating it first if needed) the ONE MaitreDQueueCounter
        row for this location. Every queue-mutating method below calls
        this FIRST, before reading or writing any MaitreDQueueEntry rows,
        so concurrent callers for the SAME location serialize through
        Postgres's row lock -- the second caller blocks until the first
        commits, then sees fresh data. (On SQLite -- tests only -- FOR
        UPDATE is a no-op since SQLite has no row-level locking, but tests
        never run concurrently against the same session anyway.)

        The bootstrap race (two transactions both find no row and both
        try to create it) is handled with a SAVEPOINT: if our insert loses
        that race, we roll back just the savepoint (not the whole
        transaction, which may already hold other pending work) and
        re-fetch-with-lock, trusting the winner's row."""
        from sqlalchemy.exc import IntegrityError
        from app.core.db import MaitreDQueueCounter

        key = loc_id or 0
        row = db.query(MaitreDQueueCounter).filter(
            MaitreDQueueCounter.store_id == self.store_id,
            MaitreDQueueCounter.location_id == key,
        ).with_for_update().first()
        if row is not None:
            return row
        try:
            with db.begin_nested():
                row = MaitreDQueueCounter(
                    store_id=self.store_id, location_id=key,
                    last_number=0, last_day=today or datetime.utcnow().date(),
                )
                db.add(row)
                db.flush()
            return row
        except IntegrityError:
            return db.query(MaitreDQueueCounter).filter(
                MaitreDQueueCounter.store_id == self.store_id,
                MaitreDQueueCounter.location_id == key,
            ).with_for_update().first()

    def _next_queue_number(self, db, loc_id: int | None, today: date) -> int:
        """Must be called with this location's counter already locked
        (see _lock_location_counter) in the SAME transaction."""
        row = self._lock_location_counter(db, loc_id, today)
        if row.last_day != today:
            row.last_number = 0
            row.last_day = today
        row.last_number += 1
        db.flush()
        return row.last_number

    # ── live queue ──

    def add_queue_entry(self, entry: QueueEntry, day_start: datetime) -> QueueEntry:
        """Appends `entry` to the end of its location's live queue.
        queue_number is the next unused number for this store+location
        since `day_start` (the venue-local start of today); position is
        the next unused slot among that location's current "waiting" rows.
        """
        from app.core.db import SessionLocal, MaitreDQueueEntry
        with SessionLocal() as db:
            loc_id = entry.location_id or None
            self._lock_location_counter(db, loc_id, day_start.date())
            queue_number = self._next_queue_number(db, loc_id, day_start.date())

            waiting = db.query(MaitreDQueueEntry).filter(
                MaitreDQueueEntry.store_id == self.store_id,
                MaitreDQueueEntry.location_id == loc_id,
                MaitreDQueueEntry.status == "waiting",
            ).all()
            position = max((r.position for r in waiting), default=0) + 1

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
        from app.core.db import SessionLocal, MaitreDQueueEntry
        with SessionLocal() as db:
            loc_id = entry.location_id or None
            self._lock_location_counter(db, loc_id, day_start.date())
            queue_number = self._next_queue_number(db, loc_id, day_start.date())

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
        for the caller to notify once committed. Callers must already
        hold this location's counter lock (see _lock_location_counter)."""
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

    def admit_next(
        self, location_id: int | None, now: datetime | None = None,
    ) -> tuple[QueueEntry | None, list[QueueEntry]]:
        """Pops position 1 (the guest who's been waiting longest at the
        front) -- the "restaurant has given them seating" action. Returns
        (admitted_entry_or_None, moved_up) -- `moved_up` is everyone whose
        position advanced, for the caller to notify.

        `now` should be the VENUE-local clock (VenueConfig.now()), not raw
        UTC -- admitted_at is later compared against MaitreD._seated_entry's
        self._now(), which is also venue-local. Mixing UTC here with a
        UTC+5 (Asia/Karachi) venue clock there would skew the "still
        seated" grace window by the timezone offset."""
        from app.core.db import SessionLocal, MaitreDQueueEntry
        with SessionLocal() as db:
            self._lock_location_counter(db, location_id)
            row = db.query(MaitreDQueueEntry).filter(
                MaitreDQueueEntry.store_id == self.store_id,
                MaitreDQueueEntry.location_id == (location_id or None),
                MaitreDQueueEntry.status == "waiting",
            ).order_by(MaitreDQueueEntry.position).first()
            if not row:
                return None, []
            moved_up = self._close_gap(db, row)
            row.status = "admitted"
            row.admitted_at = now or datetime.utcnow()
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
            self._lock_location_counter(db, location_id)
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
        """Guest-initiated leave ("cancel"), or the maintenance sweep
        expiring a stale entry -- looked up by row id, not position, since
        neither caller knows (or should need to know) the numeric slot."""
        from app.core.db import SessionLocal, MaitreDQueueEntry
        with SessionLocal() as db:
            # location_id is needed to know which counter to lock, but
            # isn't known until we've read the row -- a plain (unlocked)
            # read of just that column is safe since it never changes
            # once set, and the actual mutation below re-reads the row
            # WITH the lock held before trusting its "waiting" status.
            preview = db.query(MaitreDQueueEntry.location_id).filter(
                MaitreDQueueEntry.store_id == self.store_id,
                MaitreDQueueEntry.id == entry_id,
            ).first()
            if not preview:
                return None, []
            self._lock_location_counter(db, preview[0])
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

    def update_waiting_entry(
        self, entry_id: int, party_size: int | None = None, name: str | None = None,
    ) -> QueueEntry | None:
        """Guest-initiated "actually we're 5 now" / "put it under Bilal"
        -- updates an EXISTING waiting entry in place (same position,
        same queue_number) instead of the guest having to cancel and
        rejoin at the back of the line."""
        from app.core.db import SessionLocal, MaitreDQueueEntry
        with SessionLocal() as db:
            row = db.query(MaitreDQueueEntry).filter(
                MaitreDQueueEntry.store_id == self.store_id,
                MaitreDQueueEntry.id == entry_id,
                MaitreDQueueEntry.status == "waiting",
            ).first()
            if not row:
                return None
            if party_size:
                row.party_size = party_size
            if name:
                row.name = name
            db.commit()
            db.refresh(row)
            return self._row_to_queue_entry(row)

    def latest_waiting_entry_for(self, phone: str) -> QueueEntry | None:
        from app.core.db import SessionLocal, MaitreDQueueEntry
        with SessionLocal() as db:
            row = db.query(MaitreDQueueEntry).filter(
                MaitreDQueueEntry.store_id == self.store_id,
                MaitreDQueueEntry.phone == phone,
                MaitreDQueueEntry.status == "waiting",
            ).order_by(MaitreDQueueEntry.created_at.desc()).first()
            return self._row_to_queue_entry(row) if row else None

    def latest_admitted_entry_for(self, phone: str) -> QueueEntry | None:
        """Most recent entry this phone was actually seated from -- the
        "are they currently dining" signal a table QR's "book" guard reads
        (see MaitreD._seated_entry)."""
        from app.core.db import SessionLocal, MaitreDQueueEntry
        with SessionLocal() as db:
            row = db.query(MaitreDQueueEntry).filter(
                MaitreDQueueEntry.store_id == self.store_id,
                MaitreDQueueEntry.phone == phone,
                MaitreDQueueEntry.status == "admitted",
            ).order_by(MaitreDQueueEntry.admitted_at.desc()).first()
            return self._row_to_queue_entry(row) if row else None

    def stale_waiting_entries(self, location_id: int | None, cutoff: datetime) -> list[QueueEntry]:
        """"waiting" entries at this location that have sat untouched
        since at or before `cutoff` -- the maintenance sweep's candidates
        for auto-release (see agent.py's expire_stale_entries)."""
        from app.core.db import SessionLocal, MaitreDQueueEntry
        with SessionLocal() as db:
            rows = db.query(MaitreDQueueEntry).filter(
                MaitreDQueueEntry.store_id == self.store_id,
                MaitreDQueueEntry.location_id == (location_id or None),
                MaitreDQueueEntry.status == "waiting",
                MaitreDQueueEntry.created_at <= cutoff,
            ).all()
            return [self._row_to_queue_entry(r) for r in rows]

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
            phone=row.phone or "", name=row.name, party_size=row.party_size,
            special_requests=row.special_requests or "", status=row.status,
            position=row.position, is_vip=row.is_vip, vip_tier=row.vip_tier,
            created_at=row.created_at, admitted_at=row.admitted_at,
        )

    # ── entrance codes (one-time-use QR relinker tokens) ──

    def generate_entrance_code(self, location_id: int | None, length: int = 8) -> str:
        """Mints a fresh, unused code for this store/location -- called by
        the /q/{store_id} relinker on every single visit, never by the QR
        image itself (which only ever encodes the relinker's own static
        URL). Collision retry is a formality (26**36^8 space), not a real
        concern at this volume, but cheap to guard anyway."""
        import secrets
        import string
        from app.core.db import SessionLocal, MaitreDEntranceCode

        alphabet = string.ascii_uppercase + string.digits
        with SessionLocal() as db:
            for _ in range(5):
                code = "".join(secrets.choice(alphabet) for _ in range(length))
                if not db.query(MaitreDEntranceCode).filter(
                    MaitreDEntranceCode.code == code
                ).first():
                    break
            db.add(MaitreDEntranceCode(
                store_id=self.store_id, location_id=location_id or None, code=code,
            ))
            db.commit()
            return code

    def peek_entrance_code_location(self, code: str) -> tuple[bool, int]:
        """Read-only lookup of which location a code was minted for,
        WITHOUT consuming it -- lets a caller check that branch's own
        booking_enabled setting before committing to redeem_entrance_code
        (burning it), so a scan of a currently-closed branch doesn't waste
        an otherwise-still-valid code. Same validity rules as
        redeem_entrance_code (must exist, be unused, and be within its
        TTL); (False, 0) covers all three "not valid" cases identically,
        since a caller only ever needs to know "can I trust this code
        right now", not why not."""
        from datetime import timedelta
        from app.core.db import SessionLocal, MaitreDEntranceCode
        from app.agents.maitre_d.config import get_entrance_code_ttl_minutes

        ttl = get_entrance_code_ttl_minutes(self.store_id)
        cutoff = datetime.utcnow() - timedelta(minutes=ttl)
        with SessionLocal() as db:
            row = db.query(MaitreDEntranceCode).filter(
                MaitreDEntranceCode.store_id == self.store_id,
                MaitreDEntranceCode.code == code,
                MaitreDEntranceCode.used_at.is_(None),
                MaitreDEntranceCode.created_at >= cutoff,
            ).first()
            if not row:
                return False, 0
            return True, row.location_id or 0

    def redeem_entrance_code(self, code: str) -> tuple[bool, int]:
        """Atomically claims a one-time entrance code. Returns (ok,
        location_id) -- ok is False if the code doesn't exist, belongs to
        a different store, was already used, or is past its TTL; True and
        the location it was minted for (0 = the store's single implicit
        location) otherwise.

        The UPDATE...WHERE is the atomicity boundary, not a preceding
        SELECT check: two requests racing to redeem the identical code
        (a guest double-tapping "send", or a screenshot both they and a
        friend try at once) can never both succeed, since the second
        UPDATE's WHERE clause simply matches zero rows once the first has
        committed -- a SELECT-then-UPDATE pair would leave a window where
        both could see "still unused" first."""
        from datetime import timedelta
        from sqlalchemy import update as sa_update
        from app.core.db import SessionLocal, MaitreDEntranceCode
        from app.agents.maitre_d.config import get_entrance_code_ttl_minutes

        ttl = get_entrance_code_ttl_minutes(self.store_id)
        cutoff = datetime.utcnow() - timedelta(minutes=ttl)
        with SessionLocal() as db:
            row = db.query(MaitreDEntranceCode).filter(
                MaitreDEntranceCode.store_id == self.store_id,
                MaitreDEntranceCode.code == code,
            ).first()
            if not row:
                return False, 0
            result = db.execute(
                sa_update(MaitreDEntranceCode)
                .where(
                    MaitreDEntranceCode.id == row.id,
                    MaitreDEntranceCode.used_at.is_(None),
                    MaitreDEntranceCode.created_at >= cutoff,
                )
                .values(used_at=datetime.utcnow())
            )
            db.commit()
            if result.rowcount == 0:
                return False, 0
            return True, row.location_id or 0

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
