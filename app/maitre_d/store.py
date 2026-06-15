"""SQLite persistence for the Maître d'.

Holds reservations, the waitlist, guest records and per-phone conversation
state. Pass ``":memory:"`` for tests, or a file path for a durable store.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta

from app.maitre_d.models import Guest, Reservation, WaitlistEntry

# Statuses that still occupy a table (so they count against capacity).
ACTIVE_RESERVATION_STATUSES = ("pending", "confirmed", "seated")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(dt: str | None) -> datetime | None:
    return datetime.fromisoformat(dt) if dt else None


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        # check_same_thread=False so the FastAPI app can share one store.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS guests (
                phone      TEXT PRIMARY KEY,
                name       TEXT DEFAULT '',
                vip_tier   TEXT DEFAULT '',
                vip_notes  TEXT DEFAULT '',
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS reservations (
                reservation_id   TEXT PRIMARY KEY,
                phone            TEXT NOT NULL,
                name             TEXT DEFAULT '',
                party_size       INTEGER NOT NULL,
                when_at          TEXT NOT NULL,
                status           TEXT NOT NULL,
                table_id         TEXT DEFAULT '',
                is_vip           INTEGER DEFAULT 0,
                vip_tier         TEXT DEFAULT '',
                no_show_risk     REAL DEFAULT 0,
                no_show_band     TEXT DEFAULT 'low',
                deposit_required INTEGER DEFAULT 0,
                special_requests TEXT DEFAULT '',
                source           TEXT DEFAULT 'whatsapp',
                created_at       TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_res_when ON reservations(when_at);
            CREATE INDEX IF NOT EXISTS idx_res_phone ON reservations(phone);

            CREATE TABLE IF NOT EXISTS waitlist (
                waitlist_id    TEXT PRIMARY KEY,
                phone          TEXT NOT NULL,
                name           TEXT DEFAULT '',
                party_size     INTEGER NOT NULL,
                requested_when TEXT NOT NULL,
                status         TEXT NOT NULL,
                is_vip         INTEGER DEFAULT 0,
                vip_tier       TEXT DEFAULT '',
                offered_at     TEXT,
                created_at     TEXT
            );

            CREATE TABLE IF NOT EXISTS conversations (
                phone      TEXT PRIMARY KEY,
                state      TEXT DEFAULT '{}',
                updated_at TEXT
            );
            """
        )
        self.conn.commit()

    # ── guests ──

    def upsert_guest(self, guest: Guest) -> None:
        self.conn.execute(
            """INSERT INTO guests (phone, name, vip_tier, vip_notes, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(phone) DO UPDATE SET
                   name=excluded.name,
                   vip_tier=excluded.vip_tier,
                   vip_notes=excluded.vip_notes
               WHERE excluded.name != '' OR excluded.vip_tier != ''""",
            (guest.phone, guest.name, guest.vip_tier, guest.vip_notes,
             _iso(guest.created_at)),
        )
        self.conn.commit()

    def get_guest(self, phone: str) -> Guest | None:
        row = self.conn.execute(
            "SELECT * FROM guests WHERE phone = ?", (phone,)
        ).fetchone()
        if not row:
            return None
        return Guest(
            phone=row["phone"], name=row["name"], vip_tier=row["vip_tier"],
            vip_notes=row["vip_notes"], created_at=_parse(row["created_at"]),
        )

    # ── reservations ──

    def add_reservation(self, res: Reservation) -> Reservation:
        if not res.reservation_id:
            res.reservation_id = _new_id("res")
        self.conn.execute(
            """INSERT INTO reservations (
                reservation_id, phone, name, party_size, when_at, status,
                table_id, is_vip, vip_tier, no_show_risk, no_show_band,
                deposit_required, special_requests, source, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (res.reservation_id, res.phone, res.name, res.party_size,
             _iso(res.when), res.status, res.table_id, int(res.is_vip),
             res.vip_tier, res.no_show_risk, res.no_show_band,
             int(res.deposit_required), res.special_requests, res.source,
             _iso(res.created_at)),
        )
        self.conn.commit()
        return res

    def update_reservation_status(self, reservation_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE reservations SET status = ? WHERE reservation_id = ?",
            (status, reservation_id),
        )
        self.conn.commit()

    def get_reservation(self, reservation_id: str) -> Reservation | None:
        row = self.conn.execute(
            "SELECT * FROM reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        return self._row_to_reservation(row) if row else None

    def latest_active_reservation_for(self, phone: str) -> Reservation | None:
        row = self.conn.execute(
            f"""SELECT * FROM reservations
                WHERE phone = ? AND status IN ({_qmarks(ACTIVE_RESERVATION_STATUSES)})
                ORDER BY when_at ASC LIMIT 1""",
            (phone, *ACTIVE_RESERVATION_STATUSES),
        ).fetchone()
        return self._row_to_reservation(row) if row else None

    def reservation_history_for(self, phone: str) -> list[Reservation]:
        rows = self.conn.execute(
            "SELECT * FROM reservations WHERE phone = ? ORDER BY when_at",
            (phone,),
        ).fetchall()
        return [self._row_to_reservation(r) for r in rows]

    def active_reservations_overlapping(
        self, when: datetime, turn_minutes: int
    ) -> list[Reservation]:
        """Active reservations whose hold window overlaps ``[when, when+turn)``.

        Two intervals overlap iff each starts before the other ends. We over-fetch
        a day around the target and filter in Python to keep the SQL simple.
        """
        new_start = when
        new_end = when + timedelta(minutes=turn_minutes)
        lo = (when - timedelta(hours=12)).isoformat()
        hi = (when + timedelta(hours=12)).isoformat()
        rows = self.conn.execute(
            f"""SELECT * FROM reservations
                WHERE status IN ({_qmarks(ACTIVE_RESERVATION_STATUSES)})
                  AND when_at >= ? AND when_at <= ?""",
            (*ACTIVE_RESERVATION_STATUSES, lo, hi),
        ).fetchall()
        out = []
        for r in rows:
            res = self._row_to_reservation(r)
            existing_start = res.when
            existing_end = res.when + timedelta(minutes=turn_minutes)
            if new_start < existing_end and existing_start < new_end:
                out.append(res)
        return out

    def list_reservations(
        self, status: str | None = None
    ) -> list[Reservation]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM reservations WHERE status = ? ORDER BY when_at",
                (status,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM reservations ORDER BY when_at"
            ).fetchall()
        return [self._row_to_reservation(r) for r in rows]

    def _row_to_reservation(self, row: sqlite3.Row) -> Reservation:
        return Reservation(
            reservation_id=row["reservation_id"], phone=row["phone"],
            name=row["name"], party_size=row["party_size"],
            when=_parse(row["when_at"]), status=row["status"],
            table_id=row["table_id"], is_vip=bool(row["is_vip"]),
            vip_tier=row["vip_tier"], no_show_risk=row["no_show_risk"],
            no_show_band=row["no_show_band"],
            deposit_required=bool(row["deposit_required"]),
            special_requests=row["special_requests"], source=row["source"],
            created_at=_parse(row["created_at"]),
        )

    # ── waitlist ──

    def add_waitlist(self, entry: WaitlistEntry) -> WaitlistEntry:
        if not entry.waitlist_id:
            entry.waitlist_id = _new_id("wl")
        self.conn.execute(
            """INSERT INTO waitlist (
                waitlist_id, phone, name, party_size, requested_when, status,
                is_vip, vip_tier, offered_at, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (entry.waitlist_id, entry.phone, entry.name, entry.party_size,
             _iso(entry.requested_when), entry.status, int(entry.is_vip),
             entry.vip_tier, _iso(entry.offered_at) if entry.offered_at else None,
             _iso(entry.created_at)),
        )
        self.conn.commit()
        return entry

    def update_waitlist_status(
        self, waitlist_id: str, status: str, offered_at: datetime | None = None
    ) -> None:
        if offered_at is not None:
            self.conn.execute(
                "UPDATE waitlist SET status = ?, offered_at = ? WHERE waitlist_id = ?",
                (status, _iso(offered_at), waitlist_id),
            )
        else:
            self.conn.execute(
                "UPDATE waitlist SET status = ? WHERE waitlist_id = ?",
                (status, waitlist_id),
            )
        self.conn.commit()

    def waiting_entries_near(
        self, when: datetime, window_hours: int = 2
    ) -> list[WaitlistEntry]:
        """People still waiting whose requested time is near ``when``.

        VIPs first, then earliest-requested first (fair queue).
        """
        lo = (when - timedelta(hours=window_hours)).isoformat()
        hi = (when + timedelta(hours=window_hours)).isoformat()
        rows = self.conn.execute(
            """SELECT * FROM waitlist
               WHERE status = 'waiting'
                 AND requested_when >= ? AND requested_when <= ?
               ORDER BY is_vip DESC, created_at ASC""",
            (lo, hi),
        ).fetchall()
        return [self._row_to_waitlist(r) for r in rows]

    def list_waitlist(self, status: str | None = None) -> list[WaitlistEntry]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM waitlist WHERE status = ? ORDER BY created_at",
                (status,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM waitlist ORDER BY created_at"
            ).fetchall()
        return [self._row_to_waitlist(r) for r in rows]

    def _row_to_waitlist(self, row: sqlite3.Row) -> WaitlistEntry:
        return WaitlistEntry(
            waitlist_id=row["waitlist_id"], phone=row["phone"], name=row["name"],
            party_size=row["party_size"], requested_when=_parse(row["requested_when"]),
            status=row["status"], is_vip=bool(row["is_vip"]),
            vip_tier=row["vip_tier"], offered_at=_parse(row["offered_at"]),
            created_at=_parse(row["created_at"]),
        )

    # ── conversation state (slot-filling across messages) ──

    def get_conversation(self, phone: str) -> dict:
        row = self.conn.execute(
            "SELECT state FROM conversations WHERE phone = ?", (phone,)
        ).fetchone()
        return json.loads(row["state"]) if row else {}

    def set_conversation(self, phone: str, state: dict) -> None:
        self.conn.execute(
            """INSERT INTO conversations (phone, state, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(phone) DO UPDATE SET
                   state=excluded.state, updated_at=excluded.updated_at""",
            (phone, json.dumps(state), _iso(datetime.now())),
        )
        self.conn.commit()

    def clear_conversation(self, phone: str) -> None:
        self.conn.execute("DELETE FROM conversations WHERE phone = ?", (phone,))
        self.conn.commit()


def _qmarks(items) -> str:
    return ",".join("?" for _ in items)
