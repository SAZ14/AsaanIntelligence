"""Weekly leaderboard broadcast — runs for all stores."""
from __future__ import annotations

import logging

from app.agents.customer.community.leaderboard import format_leaderboard, weekly_stamp_counts
from app.agents.customer.community.store import load_members, load_venue_config
from app.agents.customer.services.messaging import send_whatsapp_text

logger = logging.getLogger(__name__)


def _active_store_ids() -> list[int]:
    from app.core.db import SessionLocal, Store
    with SessionLocal() as db:
        return [s.id for s in db.query(Store).all()]


def _get_store_twilio_number(store_id: int) -> str | None:
    from app.core.db import SessionLocal, StoreTwilioNumber
    with SessionLocal() as db:
        row = db.query(StoreTwilioNumber).filter(StoreTwilioNumber.store_id == store_id).first()
        return row.whatsapp_number if row else None


def broadcast_for_store(store_id: int) -> int:
    from_number = _get_store_twilio_number(store_id)
    if not from_number:
        logger.warning("No Twilio number for store %d, skipping broadcast", store_id)
        return 0
    members = load_members(store_id)
    counts = weekly_stamp_counts(store_id)
    config = load_venue_config(store_id)
    message = format_leaderboard(counts, members, title=f"{config.venue_name} — This week's top collectors")
    sent = 0
    for member in members.values():
        if not member.opted_in:
            continue
        try:
            send_whatsapp_text(member.phone, message, from_key="_OVERRIDE_")
            sent += 1
        except Exception as e:
            logger.warning("Broadcast failed store=%d phone=%s: %s", store_id, member.phone[-4:], e)
    return sent


def broadcast_all() -> None:
    for store_id in _active_store_ids():
        try:
            n = broadcast_for_store(store_id)
            logger.info("Leaderboard broadcast store=%d sent=%d", store_id, n)
        except Exception as e:
            logger.error("Broadcast error store=%d: %s", store_id, e)
