"""SQLite persistence for the Revenue agent: owner digest subscriptions and a
log of recommended campaigns. Pass ``":memory:"`` for tests.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

from app.revenue.models import CampaignLogEntry, OwnerSubscription


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS owner_subscriptions (
                phone      TEXT PRIMARY KEY,
                name       TEXT DEFAULT '',
                cadence    TEXT DEFAULT 'weekly',
                hour       INTEGER DEFAULT 9,
                active     INTEGER DEFAULT 1,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS campaign_log (
                campaign_id          TEXT PRIMARY KEY,
                created_at           TEXT,
                window_desc          TEXT DEFAULT '',
                campaign_name        TEXT DEFAULT '',
                target_segment       TEXT DEFAULT '',
                audience_size        INTEGER DEFAULT 0,
                expected_redemptions INTEGER DEFAULT 0,
                est_added_revenue    REAL DEFAULT 0,
                status               TEXT DEFAULT 'suggested'
            );
            """
        )
        self.conn.commit()

    # ── subscriptions ──

    def upsert_subscription(self, sub: OwnerSubscription) -> None:
        self.conn.execute(
            """INSERT INTO owner_subscriptions (phone, name, cadence, hour, active, created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(phone) DO UPDATE SET
                   name=excluded.name, cadence=excluded.cadence,
                   hour=excluded.hour, active=excluded.active""",
            (sub.phone, sub.name, sub.cadence, sub.hour, int(sub.active),
             _iso(sub.created_at)),
        )
        self.conn.commit()

    def get_subscription(self, phone: str) -> OwnerSubscription | None:
        row = self.conn.execute(
            "SELECT * FROM owner_subscriptions WHERE phone = ?", (phone,)
        ).fetchone()
        if not row:
            return None
        return OwnerSubscription(
            phone=row["phone"], name=row["name"], cadence=row["cadence"],
            hour=row["hour"], active=bool(row["active"]),
            created_at=_parse(row["created_at"]),
        )

    def active_subscriptions(self, cadence: str | None = None) -> list[OwnerSubscription]:
        if cadence:
            rows = self.conn.execute(
                "SELECT * FROM owner_subscriptions WHERE active = 1 AND cadence = ?",
                (cadence,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM owner_subscriptions WHERE active = 1"
            ).fetchall()
        return [
            OwnerSubscription(
                phone=r["phone"], name=r["name"], cadence=r["cadence"],
                hour=r["hour"], active=bool(r["active"]),
                created_at=_parse(r["created_at"]),
            )
            for r in rows
        ]

    # ── campaign log ──

    def log_campaign(self, entry: CampaignLogEntry) -> CampaignLogEntry:
        if not entry.campaign_id:
            entry.campaign_id = f"camp_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            """INSERT INTO campaign_log (
                campaign_id, created_at, window_desc, campaign_name, target_segment,
                audience_size, expected_redemptions, est_added_revenue, status)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (entry.campaign_id, _iso(entry.created_at), entry.window_desc,
             entry.campaign_name, entry.target_segment, entry.audience_size,
             entry.expected_redemptions, entry.est_added_revenue, entry.status),
        )
        self.conn.commit()
        return entry

    def list_campaigns(self) -> list[CampaignLogEntry]:
        rows = self.conn.execute(
            "SELECT * FROM campaign_log ORDER BY created_at DESC"
        ).fetchall()
        return [
            CampaignLogEntry(
                campaign_id=r["campaign_id"], created_at=_parse(r["created_at"]),
                window_desc=r["window_desc"], campaign_name=r["campaign_name"],
                target_segment=r["target_segment"], audience_size=r["audience_size"],
                expected_redemptions=r["expected_redemptions"],
                est_added_revenue=r["est_added_revenue"], status=r["status"],
            )
            for r in rows
        ]
