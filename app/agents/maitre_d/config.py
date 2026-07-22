"""Per-location venue capacity, service windows and VIP list for the
Maitre D.

A store can have several physical branches (Anatummy has three), each with
its own tables, service windows and no-show/deposit rules -- conflating
capacity across different addresses would let two branches' bookings
collide over a table that doesn't even exist at the other one. VenueConfig
represents ONE location; app.core.db.MaitreDLocation is the Postgres row
it's loaded from (one row per branch, same pattern as POSConnection/
RevenueConnection -- a store with no rows yet gets sensible in-code
defaults as a single implicit location, no admin step required first).

The VIP list is store-wide, not per-branch -- a VIP recognised at one
branch is still a VIP at another.
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
    name: str = "this restaurant"       # the restaurant's own name (stores.name) -- unchanged regardless of branch
    location_id: int = 0                # 0 means "no MaitreDLocation row yet, using in-code defaults"
    branch_key: str = ""
    branch_name: str = ""               # e.g. "New Blue Area" -- "" when the store has only one (implicit) location
    address: str = ""
    accepts_reservations: bool = True
    is_primary: bool = False
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
    # phone (E.164, no "whatsapp:" prefix) → VIP details -- store-wide, same dict on every location
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

    def display_name(self) -> str:
        """Restaurant name, with the branch appended only when it's
        meaningful to say (a store with just one location never shows one
        -- keeps single-branch stores' replies exactly as before)."""
        return f"{self.name} ({self.branch_name})" if self.branch_name else self.name

    # ── loading ──

    @classmethod
    def load(cls, store_id: int, branch_key: str | None = None) -> "VenueConfig":
        """Load one location's reservation config + the store's VIP list
        from Postgres, falling back to defaults for anything not yet
        configured. With branch_key=None, loads the primary location (or
        the only one, or -- for a store with no MaitreDLocation rows at
        all -- a single implicit default location)."""
        from app.core.db import SessionLocal, Store, MaitreDLocation, MaitreDVip

        cfg = cls(store_id=store_id)
        with SessionLocal() as db:
            store = db.query(Store).filter(Store.id == store_id).first()
            if store:
                cfg.name = store.name

            q = db.query(MaitreDLocation).filter(MaitreDLocation.store_id == store_id)
            row = (
                q.filter(MaitreDLocation.branch_key == branch_key).first() if branch_key
                else q.filter(MaitreDLocation.is_primary.is_(True)).first()
                or q.order_by(MaitreDLocation.id).first()
            )
            if row:
                _apply_location_row(cfg, row)
                # Only show a branch label at all when this store actually
                # has more than one location -- a lone MaitreDLocation row
                # (is_primary or not) still reads as "the restaurant", not
                # "the restaurant (Main Branch)".
                if q.count() <= 1:
                    cfg.branch_name = ""

            vips = {}
            for v in db.query(MaitreDVip).filter(MaitreDVip.store_id == store_id).all():
                vips[normalise_phone(v.phone)] = VipProfile(
                    name=v.name or "", tier=v.tier or "vip", notes=v.notes or "",
                )
            cfg.vips = vips
        return cfg

    @classmethod
    def list_locations(cls, store_id: int) -> list["VenueConfig"]:
        """All of a store's configured locations, primary first, that's
        used to offer a guest a branch choice. Empty list means the store
        hasn't configured branches at all -- callers should fall back to
        load(store_id) (single implicit location) in that case."""
        from app.core.db import SessionLocal, Store, MaitreDLocation, MaitreDVip

        with SessionLocal() as db:
            store = db.query(Store).filter(Store.id == store_id).first()
            store_name = store.name if store else "this restaurant"
            rows = db.query(MaitreDLocation).filter(
                MaitreDLocation.store_id == store_id
            ).order_by(MaitreDLocation.is_primary.desc(), MaitreDLocation.id).all()

            vips = {}
            for v in db.query(MaitreDVip).filter(MaitreDVip.store_id == store_id).all():
                vips[normalise_phone(v.phone)] = VipProfile(
                    name=v.name or "", tier=v.tier or "vip", notes=v.notes or "",
                )

            configs = []
            for row in rows:
                cfg = cls(store_id=store_id, name=store_name, vips=vips)
                _apply_location_row(cfg, row)
                if len(rows) <= 1:
                    cfg.branch_name = ""
                configs.append(cfg)
            return configs


def _apply_location_row(cfg: VenueConfig, row) -> None:
    cfg.location_id = row.id
    cfg.branch_key = row.branch_key
    cfg.branch_name = row.name
    cfg.address = row.address or ""
    cfg.accepts_reservations = row.accepts_reservations
    cfg.is_primary = row.is_primary
    cfg.timezone = row.timezone or cfg.timezone
    if row.tables:
        cfg.tables = [(t[0], int(t[1])) for t in row.tables]
    if row.service_windows:
        cfg.service_windows = [(w[0], int(w[1]), int(w[2])) for w in row.service_windows]
    cfg.turn_time_minutes = row.turn_time_minutes or cfg.turn_time_minutes
    cfg.large_party_turn_minutes = row.large_party_turn_minutes or cfg.large_party_turn_minutes
    cfg.large_party_threshold = row.large_party_threshold or cfg.large_party_threshold
    cfg.max_party_size = row.max_party_size or cfg.max_party_size
    cfg.currency = row.currency or cfg.currency
    cfg.deposit_amount = row.deposit_amount or cfg.deposit_amount
    cfg.offer_ttl_minutes = row.offer_ttl_minutes or cfg.offer_ttl_minutes
    cfg.no_show_grace_minutes = row.no_show_grace_minutes or cfg.no_show_grace_minutes
    cfg.reminder_lead_hours = row.reminder_lead_hours or cfg.reminder_lead_hours
    cfg.conversation_ttl_minutes = row.conversation_ttl_minutes or cfg.conversation_ttl_minutes


def normalise_phone(phone: str) -> str:
    """Strip channel prefixes/formatting so lookups are stable.

    "whatsapp:+92 300 1234567" → "+923001234567"
    """
    p = phone.strip().lower()
    if p.startswith("whatsapp:"):
        p = p[len("whatsapp:"):]
    return "".join(ch for ch in p if ch.isdigit() or ch == "+")
