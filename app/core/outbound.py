"""Per-store outbound sender for business-initiated WhatsApp messages.

Inbound conversations always reply on the provider the message arrived on
(the webhook builds the send function). But proactive sends — win-back
nudges, leaderboard broadcasts — start on our side, so they must ask:
"which provider does this store send from?"

Resolution order is meta → openwa → twilio: a store that completed Meta
embedded signup sends from its official Cloud API number even if legacy
registrations are still on file (e.g. the shared Twilio sandbox number),
mirroring how inbound traffic actually shifts when a number moves.
"""
from __future__ import annotations

import logging
import re
from typing import Callable

logger = logging.getLogger(__name__)


def _digits(phone: str) -> str:
    """'whatsapp:+923001234567' → '923001234567'."""
    return re.sub(r"\D", "", phone or "")


def resolve_store_sender(store_id: int) -> tuple[str, Callable[[str, str], None]] | None:
    """Return (provider_name, send_fn) for a store, or None if no provider
    is registered. send_fn takes (to_number, body); to_number accepts any
    of the formats used across the codebase ('whatsapp:+92...', '+92...',
    digits) and is normalized per provider."""
    from app.core.db import (
        SessionLocal, StoreMetaNumber, StoreOpenWASession, StoreTwilioNumber,
    )

    with SessionLocal() as db:
        meta = db.query(StoreMetaNumber).filter(
            StoreMetaNumber.store_id == store_id).first()
        if meta:
            phone_number_id = meta.phone_number_id

            def _send_via_meta(to: str, body: str) -> None:
                from app.core.meta_send import send_meta
                send_meta(phone_number_id, _digits(to), body)
            return "meta", _send_via_meta

        owa = db.query(StoreOpenWASession).filter(
            StoreOpenWASession.store_id == store_id).first()
        if owa:
            session_id = owa.session_id

            def _send_via_openwa(to: str, body: str) -> None:
                from app.core.openwa_send import send_openwa
                send_openwa(session_id, f"{_digits(to)}@c.us", body)
            return "openwa", _send_via_openwa

        tw = db.query(StoreTwilioNumber).filter(
            StoreTwilioNumber.store_id == store_id).first()
        if tw:
            from_number = tw.whatsapp_number

            def _send_via_twilio(to: str, body: str) -> None:
                from app.core.twilio_send import send_whatsapp
                send_whatsapp(to=to, body=body, from_=from_number)
            return "twilio", _send_via_twilio

    return None


def send_from_store(store_id: int, to: str, body: str) -> bool:
    """Send one business-initiated message from a store's number.
    Returns False (with a log line) when the store has no provider."""
    resolved = resolve_store_sender(store_id)
    if resolved is None:
        logger.warning("outbound: store=%d has no messaging provider registered", store_id)
        return False
    provider, send_fn = resolved
    send_fn(to, body)
    logger.info("outbound: store=%d provider=%s to=...%s", store_id, provider, _digits(to)[-4:])
    return True
