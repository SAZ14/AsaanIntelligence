"""Venue capacity, service windows and the VIP list for the Maître d'.

Everything here ships with sensible café defaults and can be overridden from a
JSON config file (see ``VenueConfig.load``) without code changes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


# ── Defaults (Sugar Rush café) ──

DEFAULT_VENUE_NAME = "Sugar Rush"
DEFAULT_TIMEZONE = "Asia/Karachi"

# Each table is (table_id, seats). Bookings are seated at the smallest table
# that fits the party. Tune freely — this is the single source of capacity truth.
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

# A table is held this long from the reservation time before it turns over.
DEFAULT_TURN_TIME_MINUTES = 90
# Large parties linger; give them a longer turn.
DEFAULT_LARGE_PARTY_TURN_MINUTES = 120
LARGE_PARTY_THRESHOLD = 6

DEFAULT_MAX_PARTY_SIZE = 12  # above this we ask the guest to call the venue

DEFAULT_CURRENCY = "PKR"
# Flat deposit asked for to secure a high-no-show-risk booking.
DEFAULT_DEPOSIT_AMOUNT = 1000

# A waitlist offer (a freed table) is held this long for the guest to accept
# before it lapses and rolls on to the next person in the queue.
DEFAULT_OFFER_TTL_MINUTES = 15
# A confirmed guest who hasn't arrived this long after their time is a no-show.
DEFAULT_NO_SHOW_GRACE_MINUTES = 30
# Send the day-before reminder this many hours ahead of the booking.
DEFAULT_REMINDER_LEAD_HOURS = 24
# Abandoned slot-filling conversations are forgotten after this long.
DEFAULT_CONVERSATION_TTL_MINUTES = 180


@dataclass
class VenueConfig:
    name: str = DEFAULT_VENUE_NAME
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
    vips: dict[str, "VipProfile"] = field(default_factory=dict)

    # ── helpers ──

    def now(self) -> datetime:
        """Current wall-clock time *in the venue's timezone*, as a naive datetime.

        Everything in the engine works in venue-local time, so this is the single
        clock the agent reads. Using the venue tz (not the server's) means
        "table for tonight 8pm" resolves correctly wherever the service runs.
        """
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

    def vip_for(self, phone: str) -> "VipProfile | None":
        return self.vips.get(_normalise_phone(phone))

    # ── loading ──

    @classmethod
    def load(cls, path: str | Path | None = None) -> "VenueConfig":
        """Load config from JSON, falling back to defaults for any missing keys.

        Returns a default café config if ``path`` is None or the file is absent.
        """
        cfg = cls()
        if path is None:
            cfg.vips = _default_vips()
            return cfg
        p = Path(path)
        if not p.exists():
            cfg.vips = _default_vips()
            return cfg

        raw = json.loads(p.read_text())
        if "name" in raw:
            cfg.name = raw["name"]
        if "timezone" in raw:
            cfg.timezone = raw["timezone"]
        if "tables" in raw:
            cfg.tables = [(t["id"], int(t["seats"])) for t in raw["tables"]]
        if "service_windows" in raw:
            cfg.service_windows = [
                (w["label"], int(w["open_hour"]), int(w["last_seating_hour"]))
                for w in raw["service_windows"]
            ]
        if "turn_time_minutes" in raw:
            cfg.turn_time_minutes = int(raw["turn_time_minutes"])
        if "large_party_turn_minutes" in raw:
            cfg.large_party_turn_minutes = int(raw["large_party_turn_minutes"])
        if "large_party_threshold" in raw:
            cfg.large_party_threshold = int(raw["large_party_threshold"])
        if "max_party_size" in raw:
            cfg.max_party_size = int(raw["max_party_size"])
        if "currency" in raw:
            cfg.currency = raw["currency"]
        if "deposit_amount" in raw:
            cfg.deposit_amount = int(raw["deposit_amount"])
        if "offer_ttl_minutes" in raw:
            cfg.offer_ttl_minutes = int(raw["offer_ttl_minutes"])
        if "no_show_grace_minutes" in raw:
            cfg.no_show_grace_minutes = int(raw["no_show_grace_minutes"])
        if "reminder_lead_hours" in raw:
            cfg.reminder_lead_hours = int(raw["reminder_lead_hours"])
        if "conversation_ttl_minutes" in raw:
            cfg.conversation_ttl_minutes = int(raw["conversation_ttl_minutes"])

        vips = {}
        for v in raw.get("vips", []):
            prof = VipProfile(
                name=v.get("name", ""),
                tier=v.get("tier", "vip"),
                notes=v.get("notes", ""),
            )
            vips[_normalise_phone(v["phone"])] = prof
        cfg.vips = vips or _default_vips()
        return cfg


@dataclass
class VipProfile:
    name: str = ""
    tier: str = "vip"          # "vip" | "regular" | "press" | "owner_friend" ...
    notes: str = ""            # e.g. "window table, allergic to nuts"


def _normalise_phone(phone: str) -> str:
    """Strip channel prefixes/formatting so lookups are stable.

    "whatsapp:+92 300 1234567" → "+923001234567"
    """
    p = phone.strip().lower()
    if p.startswith("whatsapp:"):
        p = p[len("whatsapp:"):]
    return "".join(ch for ch in p if ch.isdigit() or ch == "+")


def _default_vips() -> dict[str, VipProfile]:
    """A small built-in VIP list so the agent works out of the box."""
    return {
        "+923001112222": VipProfile(
            name="Ayesha Khan", tier="vip",
            notes="Food critic. Always offer the window table.",
        ),
        "+923004445555": VipProfile(
            name="Bilal Sheikh", tier="regular",
            notes="Twice-weekly regular. Flat white, no sugar.",
        ),
        "+923007778888": VipProfile(
            name="Sana Malik", tier="owner_friend",
            notes="Owner's family. Comp dessert, never waitlist.",
        ),
    }
