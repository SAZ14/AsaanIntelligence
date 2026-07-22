"""Per-store venue capacity, service windows and VIP list for the Maitre D.

Ported from the maitre-d-agent branch's file-backed VenueConfig: same
dataclass shape and defaults, but loaded from this store's
MaitreDVenueConfig/MaitreDVip rows (app.core.db) instead of a JSON file, so
every store gets its own reservation settings the same way POSConnection/
RevenueConnection give every store its own POS/revenue config. A store
with no row yet gets sensible defaults -- no admin step required before the
agent works.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Asia/Karachi"

# Each table is (table_id, seats). Bookings are seated at the smallest table
# that fits the party. Tune freely -- this is the single source of capacity truth.
DEFAULT_TABLES: list[tuple[str, int]] = [
    ("T1", 2), ("T2", 2), ("T3", 2), ("T4", 2),
    ("T5", 4), ("T6", 4), ("T7", 4),
    ("T8", 6), ("T9", 6),
    ("T10", 8),
]

# Reservations are only accepted that *start* inside a service window.
# (label, open_hour, last_seating_hour) on a 24h clock.
DEFAULT_SERVICE_WINDOWS: list[tuple[str, int, int]] = [
    ("lunch", 12, 15),
    ("dinner", 18, 22),
]

DEFAULT_TURN_TIME_MINUTES = 90
DEFAULT_LARGE_PARTY_TURN_MINUTES = 120
LARGE_PARTY_THRESHOLD = 6
DEFAULT_MAX_PARTY_SIZE = 12
DEFAULT_CURRENCY = "PKR"
DEFAULT_DEPOSIT_AMOUNT = 1000
DEFAULT_OFFER_TTL_MINUTES = 15
DEFAULT_NO_SHOW_GRACE_MINUTES = 30
DEFAULT_REMINDER_LEAD_HOURS = 24
DEFAULT_CONVERSATION_TTL_MINUTES = 180


@dataclass
class VipProfile:
    name: str = ""
    tier: str = "vip"          # "vip" | "regular" | "press" | "owner_friend" ...
    notes: str = ""            # e.g. "window table, allergic to nuts"


@dataclass
class VenueConfig:
    store_id: int = 0
    name: str = "this restaurant"
    timezone: str = DEFAULT_TIMEZONE
    tables: list[tuple[str, int]] = field(default_factory=lambda: list(DEFAULT_TABLES))
    service_windows: list[tuple[str, int, int]] = field(
        default_factory=lambda: list(DEFAULT_SERVICE_WINDOWS)
    )
    turn_time_minutes: int = DEFAULT_TURN_TIME_MINUTES
    large_party_turn_minutes: int = DEFAULT_LARGE_PARTY_TURN_MINUTES
    large_party_threshold: int = LARGE_PARTY_THRESHOLD
    max_party_size: int = DEFAULT_MAX_PARTY_SIZE
    currency: str = DEFAULT_CURRENCY
    deposit_amount: int = DEFAULT_DEPOSIT_AMOUNT
    offer_ttl_minutes: int = DEFAULT_OFFER_TTL_MINUTES
    no_show_grace_minutes: int = DEFAULT_NO_SHOW_GRACE_MINUTES
    reminder_lead_hours: int = DEFAULT_REMINDER_LEAD_HOURS
    conversation_ttl_minutes: int = DEFAULT_CONVERSATION_TTL_MINUTES
    # phone (E.164, no "whatsapp:" prefix) → VIP details
    vips: dict[str, VipProfile] = field(default_factory=dict)

    # ── helpers ──

    def now(self) -> datetime:
        """Current wall-clock time *in the venue's timezone*, as a naive datetime.

        Everything in the engine works in venue-local time, so this is the
        single clock the agent reads -- "table for tonight 8pm" resolves
        correctly regardless of where the server itself runs."""
        try:
            return datetime.now(ZoneInfo(self.timezone)).replace(tzinfo=None)
        except Exception:  # unknown tz name → fall back to server local time
            return datetime.now()

    def turn_time_for(self, party_size: int) -> int:
        if party_size >= self.large_party_threshold:
            return self.large_party_turn_minutes
        return self.turn_time_minutes

    def tables_fitting(self, party_size: int) -> list[tuple[str, int]]:
        """Tables that can seat the party, smallest first (best fit)."""
        return sorted(
            [(tid, seats) for tid, seats in self.tables if seats >= party_size],
            key=lambda t: t[1],
        )

    def hour_in_service_window(self, hour: int) -> str | None:
        for label, open_h, last_h in self.service_windows:
            if open_h <= hour <= last_h:
                return label
        return None

    def vip_for(self, phone: str) -> VipProfile | None:
        return self.vips.get(normalise_phone(phone))

    # ── loading ──

    @classmethod
    def load(cls, store_id: int) -> "VenueConfig":
        """Load this store's reservation config + VIP list from Postgres,
        falling back to defaults for anything not yet configured. Store
        name always comes from the stores table (single source of truth
        for restaurant identity, same as every other agent)."""
        from app.core.db import SessionLocal, Store, MaitreDVenueConfig, MaitreDVip

        cfg = cls(store_id=store_id)
        with SessionLocal() as db:
            store = db.query(Store).filter(Store.id == store_id).first()
            if store:
                cfg.name = store.name

            row = db.query(MaitreDVenueConfig).filter(
                MaitreDVenueConfig.store_id == store_id
            ).first()
            if row:
                cfg.timezone = row.timezone or cfg.timezone
                if row.tables:
                    cfg.tables = [(t[0], int(t[1])) for t in row.tables]
                if row.service_windows:
                    cfg.service_windows = [
                        (w[0], int(w[1]), int(w[2])) for w in row.service_windows
                    ]
                cfg.turn_time_minutes = row.turn_time_minutes or cfg.turn_time_minutes
                cfg.large_party_turn_minutes = (
                    row.large_party_turn_minutes or cfg.large_party_turn_minutes
                )
                cfg.large_party_threshold = row.large_party_threshold or cfg.large_party_threshold
                cfg.max_party_size = row.max_party_size or cfg.max_party_size
                cfg.currency = row.currency or cfg.currency
                cfg.deposit_amount = row.deposit_amount or cfg.deposit_amount
                cfg.offer_ttl_minutes = row.offer_ttl_minutes or cfg.offer_ttl_minutes
                cfg.no_show_grace_minutes = (
                    row.no_show_grace_minutes or cfg.no_show_grace_minutes
                )
                cfg.reminder_lead_hours = row.reminder_lead_hours or cfg.reminder_lead_hours
                cfg.conversation_ttl_minutes = (
                    row.conversation_ttl_minutes or cfg.conversation_ttl_minutes
                )

            vips = {}
            for v in db.query(MaitreDVip).filter(MaitreDVip.store_id == store_id).all():
                vips[normalise_phone(v.phone)] = VipProfile(
                    name=v.name or "", tier=v.tier or "vip", notes=v.notes or "",
                )
            cfg.vips = vips
        return cfg


def normalise_phone(phone: str) -> str:
    """Strip channel prefixes/formatting so lookups are stable.

    "whatsapp:+92 300 1234567" → "+923001234567"
    """
    p = phone.strip().lower()
    if p.startswith("whatsapp:"):
        p = p[len("whatsapp:"):]
    return "".join(ch for ch in p if ch.isdigit() or ch == "+")
