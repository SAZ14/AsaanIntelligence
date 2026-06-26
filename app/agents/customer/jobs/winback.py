"""Daily winback job — re-engage members who haven't visited recently.

Iterates all stores in the DB and sends a personalised WhatsApp nudge
to members whose last_activity_at is older than config.winback_days.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.agents.customer.community.store import load_members, load_venue_config, save_members

logger = logging.getLogger(__name__)


def _active_store_ids() -> list[int]:
    from app.core.db import SessionLocal, Store
    with SessionLocal() as db:
        return [s.id for s in db.query(Store).all()]


def _send_winback(phone: str, name: str, venue_name: str, from_number: str) -> None:
    from twilio.rest import Client
    from app.core.config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    body = (
        f"Hi {name or 'there'}! We miss you at {venue_name}. "
        "Come visit us and text your next receipt code to earn stamps."
    )
    client.messages.create(to=f"whatsapp:{phone}", from_=from_number, body=body)


def _get_store_twilio_number(store_id: int) -> str | None:
    from app.core.db import SessionLocal, StoreTwilioNumber
    with SessionLocal() as db:
        row = db.query(StoreTwilioNumber).filter(StoreTwilioNumber.store_id == store_id).first()
        return row.whatsapp_number if row else None


def run_winback_for_store(store_id: int) -> None:
    config = load_venue_config(store_id)
    from_number = _get_store_twilio_number(store_id)
    if not from_number:
        logger.warning("No Twilio number for store %d, skipping winback", store_id)
        return
    members = load_members(store_id)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=config.winback_days)
    changed = False
    for phone, m in members.items():
        if not m.opted_in:
            continue
        if not m.last_activity_at:
            continue
        last = datetime.fromisoformat(m.last_activity_at)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last >= cutoff:
            continue
        if m.winback_sent_at:
            sent = datetime.fromisoformat(m.winback_sent_at)
            if sent.tzinfo is None:
                sent = sent.replace(tzinfo=timezone.utc)
            if sent >= cutoff:
                continue
        try:
            _send_winback(phone, m.name, config.venue_name, from_number)
            m.winback_sent_at = now.isoformat()
            changed = True
            logger.info("Winback sent store=%d phone=%s", store_id, phone[-4:])
        except Exception as e:
            logger.warning("Winback failed store=%d phone=%s: %s", store_id, phone[-4:], e)
    if changed:
        save_members(store_id, members)


def run_winback_all() -> None:
    for store_id in _active_store_ids():
        try:
            run_winback_for_store(store_id)
        except Exception as e:
            logger.error("Winback error store=%d: %s", store_id, e)
