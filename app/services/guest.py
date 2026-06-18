"""Guest onboarding — permanent venue QR join and return-visit recognition."""

from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.models.canonical import LoyaltyCustomer, LoyaltyRules, MenuItem, Order, Staff
from app.services.messaging import normalize_phone


@dataclass
class VenueConfig:
    venue_id: str
    name: str
    currency: str = "PKR"
    join_slug: str = ""
    whatsapp_greeting: str = ""


@dataclass
class GuestJoinResult:
    customer_ref: str
    qr_token: str
    display_name: str
    phone: str
    venue_name: str
    venue_slug: str
    short_code: str
    is_returning: bool
    visit_count: int = 0
    loyalty_tier: str = "none"
    next_milestone: int = 0
    visits_to_milestone: int = 0
    message: str = ""


def load_venues(path: Path) -> dict[str, VenueConfig]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    venues: dict[str, VenueConfig] = {}
    for slug, data in raw.items():
        venues[slug] = VenueConfig(
            venue_id=data.get("venue_id", slug),
            name=data.get("name", slug),
            currency=data.get("currency", "PKR"),
            join_slug=data.get("join_slug", slug),
            whatsapp_greeting=data.get("whatsapp_greeting", ""),
        )
    return venues


def generate_customer_ref() -> str:
    return "C" + uuid.uuid4().hex[:10]


def generate_qr_token() -> str:
    return "QR-" + secrets.token_hex(4)


def find_by_phone(
    registry: dict[str, LoyaltyCustomer],
    phone: str,
) -> LoyaltyCustomer | None:
    normalized = normalize_phone(phone)
    for c in registry.values():
        if normalize_phone(c.phone) == normalized:
            return c
    return None


def _guest_stats(
    customer_ref: str,
    orders: list[Order],
    rules: LoyaltyRules,
) -> tuple[int, str, int, int]:
    """Return visit_count, loyalty_tier, next_milestone, visits_to_milestone."""
    visits = {
        o.datetime.date() for o in orders
        if o.customer_ref == customer_ref
    }
    visit_count = len(visits)
    tier = "none"
    if visit_count >= 35:
        tier = "platinum"
    elif visit_count >= 20:
        tier = "gold"
    elif visit_count >= 10:
        tier = "silver"
    elif visit_count >= 3:
        tier = "bronze"
    interval = rules.milestone_visit_interval
    if visit_count == 0:
        next_ms = interval
        to_go = interval
    else:
        remainder = visit_count % interval
        if remainder == 0:
            next_ms = visit_count + interval
            to_go = interval
        else:
            next_ms = visit_count + (interval - remainder)
            to_go = interval - remainder
    return visit_count, tier, next_ms, to_go


def join_guest(
    venue: VenueConfig,
    registry: dict[str, LoyaltyCustomer],
    display_name: str,
    phone: str,
    orders: list[Order],
    rules: LoyaltyRules,
    *,
    opted_in: bool = True,
    channel: str = "whatsapp",
) -> GuestJoinResult:
    """Register or recognize a guest from the permanent venue QR join page."""
    phone_norm = normalize_phone(phone)
    existing = find_by_phone(registry, phone_norm)
    is_returning = existing is not None

    if existing:
        existing.display_name = display_name or existing.display_name
        existing.opted_in = opted_in
        existing.channel = channel
        entry = existing
    else:
        cref = generate_customer_ref()
        entry = LoyaltyCustomer(
            customer_ref=cref,
            qr_token=generate_qr_token(),
            display_name=display_name,
            phone=phone_norm,
            channel=channel,
            opted_in=opted_in,
        )
        registry[cref] = entry

    visit_count, tier, next_ms, to_go = _guest_stats(entry.customer_ref, orders, rules)
    short_code = entry.customer_ref[-4:].upper()

    if is_returning:
        msg = (
            f"Welcome back, {entry.display_name}! "
            f"Visit {visit_count} — {to_go} more until your next reward."
        )
    else:
        msg = (
            f"Welcome to {venue.name}, {entry.display_name}! "
            f"Show code {short_code} at checkout. Rewards via WhatsApp."
        )

    return GuestJoinResult(
        customer_ref=entry.customer_ref,
        qr_token=entry.qr_token,
        display_name=entry.display_name,
        phone=entry.phone,
        venue_name=venue.name,
        venue_slug=venue.join_slug,
        short_code=short_code,
        is_returning=is_returning,
        visit_count=visit_count,
        loyalty_tier=tier,
        next_milestone=next_ms,
        visits_to_milestone=to_go,
        message=msg,
    )


def recognize_guest(
    venue: VenueConfig,
    registry: dict[str, LoyaltyCustomer],
    phone: str,
    orders: list[Order],
    rules: LoyaltyRules,
) -> GuestJoinResult | None:
    """Look up returning guest by phone without re-registering."""
    existing = find_by_phone(registry, phone)
    if existing is None:
        return None
    visit_count, tier, next_ms, to_go = _guest_stats(existing.customer_ref, orders, rules)
    return GuestJoinResult(
        customer_ref=existing.customer_ref,
        qr_token=existing.qr_token,
        display_name=existing.display_name,
        phone=existing.phone,
        venue_name=venue.name,
        venue_slug=venue.join_slug,
        short_code=existing.customer_ref[-4:].upper(),
        is_returning=True,
        visit_count=visit_count,
        loyalty_tier=tier,
        next_milestone=next_ms,
        visits_to_milestone=to_go,
        message=f"Welcome back! Visit {visit_count} — {to_go} more until your next reward.",
    )
