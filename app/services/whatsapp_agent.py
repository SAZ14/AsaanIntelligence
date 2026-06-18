"""WhatsApp Customer Agent — onboard guests via chat (phone from Twilio, name in conversation)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from app.ingest.loader import save_customers
from app.models.canonical import LoyaltyCustomer, LoyaltyRules
from app.services.guest import (
    VenueConfig,
    find_by_phone,
    join_guest,
    load_venues,
    recognize_guest,
)
from app.services.messaging import normalize_phone, parse_twilio_whatsapp_phone, send_whatsapp_text
from app.services.whatsapp_sessions import clear_session, get_session, set_awaiting_name


@dataclass
class AgentReply:
    body: str
    registered: bool = False
    customer_ref: str = ""


def build_whatsapp_qr_url(
    whatsapp_number: str,
    venue_name: str,
    *,
    greeting: str = "",
) -> str:
    """Build wa.me link for permanent table QR — opens WhatsApp chat with prefilled hello."""
    digits = parse_twilio_whatsapp_phone(whatsapp_number).lstrip("+")
    text = greeting or f"Hi {venue_name}! I'd like to join rewards."
    return f"https://wa.me/{digits}?text={quote(text)}"


def _clean_name(raw: str) -> str:
    name = " ".join(raw.strip().split())
    if len(name) < 2:
        raise ValueError("Please send your name (at least 2 characters).")
    if len(name) > 80:
        raise ValueError("That name is too long — please send a shorter name.")
    return name


def handle_incoming_whatsapp(
    phone: str,
    message_body: str,
    *,
    venue: VenueConfig,
    registry: dict[str, LoyaltyCustomer],
    orders: list,
    rules: LoyaltyRules,
    sessions_path: Path,
    registry_path: Path,
) -> AgentReply:
    """Process an inbound WhatsApp message and return the agent reply text."""
    phone_norm = normalize_phone(parse_twilio_whatsapp_phone(phone))
    body = (message_body or "").strip()

    existing = find_by_phone(registry, phone_norm)
    if existing:
        clear_session(sessions_path, phone_norm)
        result = recognize_guest(venue, registry, phone_norm, orders, rules)
        if result is None:
            result = join_guest(
                venue, registry, existing.display_name, phone_norm, orders, rules,
            )
        save_customers(registry_path, registry)
        return AgentReply(body=result.message, customer_ref=result.customer_ref)

    session = get_session(sessions_path, phone_norm)
    if session is None or session.state != "awaiting_name":
        set_awaiting_name(sessions_path, phone_norm, venue.join_slug)
        return AgentReply(
            body=(
                f"Welcome to {venue.name}! 🎉\n\n"
                "You're connected on WhatsApp — we already have your number.\n"
                "What name should we use for your rewards?"
            ),
        )

    try:
        display_name = _clean_name(body)
    except ValueError as e:
        return AgentReply(body=str(e))

    result = join_guest(
        venue, registry, display_name, phone_norm, orders, rules,
        opted_in=True, channel="whatsapp",
    )
    save_customers(registry_path, registry)
    clear_session(sessions_path, phone_norm)
    return AgentReply(
        body=result.message,
        registered=True,
        customer_ref=result.customer_ref,
    )


def process_and_reply(
    from_phone: str,
    message_body: str,
    *,
    venue_slug: str,
    venues_path: Path,
    registry: dict[str, LoyaltyCustomer],
    orders: list,
    rules: LoyaltyRules,
    sessions_path: Path,
    registry_path: Path,
    use_twilio: bool | None = None,
) -> AgentReply:
    """Handle inbound message and send reply via Twilio (or console in dev)."""
    venues = load_venues(venues_path)
    if venue_slug not in venues:
        raise ValueError(f"Unknown venue: {venue_slug}")
    venue = venues[venue_slug]

    reply = handle_incoming_whatsapp(
        from_phone,
        message_body,
        venue=venue,
        registry=registry,
        orders=orders,
        rules=rules,
        sessions_path=sessions_path,
        registry_path=registry_path,
    )
    send_whatsapp_text(from_phone, reply.body, use_twilio=use_twilio)
    return reply
