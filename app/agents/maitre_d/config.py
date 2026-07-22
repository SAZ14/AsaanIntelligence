"""Per-location venue identity and VIP list for the Maitre D.

A store can have several physical branches (Anatummy has three), each
possibly a different queue -- conflating them would let a guest at one
branch see/queue-jump another branch's line. VenueConfig represents ONE
location; app.core.db.MaitreDLocation is the Postgres row it's loaded from
(one row per branch, same pattern as POSConnection/RevenueConnection -- a
store with no rows yet gets sensible in-code defaults as a single implicit
location, no admin step required first).

The VIP list is store-wide, not per-branch -- a VIP recognised at one
branch is still a VIP at another.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Asia/Karachi"
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
    accepts_reservations: bool = True   # can guests queue/book at this branch at all (false = delivery-only)
    is_primary: bool = False
    timezone: str = DEFAULT_TIMEZONE
    conversation_ttl_minutes: int = DEFAULT_CONVERSATION_TTL_MINUTES
    # phone (E.164, no "whatsapp:" prefix) → VIP details -- store-wide, same dict on every location
    vips: dict[str, VipProfile] = field(default_factory=dict)

    # ── helpers ──

    def now(self) -> datetime:
        """Current wall-clock time *in the venue's timezone*, as a naive datetime.

        Everything in the engine works in venue-local time, so this is the
        single clock the agent reads -- a queue's "today" resolves
        correctly regardless of where the server itself runs."""
        try:
            return datetime.now(ZoneInfo(self.timezone)).replace(tzinfo=None)
        except Exception:  # unknown tz name → fall back to server local time
            return datetime.now()

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
        """Load one location's identity + the store's VIP list from
        Postgres, falling back to defaults for anything not yet configured.
        With branch_key=None, loads the primary location (or the only one,
        or -- for a store with no MaitreDLocation rows at all -- a single
        implicit default location)."""
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
    cfg.conversation_ttl_minutes = row.conversation_ttl_minutes or cfg.conversation_ttl_minutes


def normalise_phone(phone: str) -> str:
    """Strip channel prefixes/formatting so lookups are stable.

    "whatsapp:+92 300 1234567" → "+923001234567"
    """
    p = phone.strip().lower()
    if p.startswith("whatsapp:"):
        p = p[len("whatsapp:"):]
    return "".join(ch for ch in p if ch.isdigit() or ch == "+")


# ── branch-name matching (shared by the guest booking flow and staff
#    queue commands -- both need to turn free text like "at F-8/2" into
#    one of a store's configured VenueConfig rows) ──

def _alnum(text: str) -> str:
    """Lowercase, strip everything but letters/digits -- so "F-8/2" and
    "at F-8/2," match regardless of hyphens, slashes or punctuation."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _join_or(items: list[str]) -> str:
    """["A"] -> "A"; ["A", "B"] -> "A or B"; ["A", "B", "C"] -> "A, B or C"."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" or {items[-1]}"


def match_location(locations: list[VenueConfig], text: str) -> VenueConfig | None:
    """Cheap keyword match against a store's own branch names/keys. Branch
    names are store-specific vocabulary (e.g. "Bahria Town", "F-8/2"), not
    something a generic NLU module can know about. Punctuation-insensitive
    on both sides ("F-8/2" must match "at F-8/2" and "at f82" alike).
    Checks ALL locations, including ones that don't accept guests, so a
    caller can give a specific "that branch is delivery-only" answer
    instead of silently failing to match."""
    needle = _alnum(text)
    for loc in locations:
        if _alnum(loc.branch_key) in needle or _alnum(loc.branch_name) in needle:
            return loc
    return None
