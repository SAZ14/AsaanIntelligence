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
    from app.core.entitlements import filter_entitled
    with SessionLocal() as db:
        store_ids = [s.id for s in db.query(Store).all()]
    return filter_entitled(store_ids, "customer")


def _send_winback(send_fn, phone: str, name: str, venue_name: str) -> None:
    body = (
        f"Hi {name or 'there'}! We miss you at {venue_name}. "
        "Come visit us and text your next receipt code to earn stamps."
    )
    send_fn(phone, body)


def run_winback_for_store(store_id: int) -> None:
    from app.core.outbound import resolve_store_sender
    config = load_venue_config(store_id)
    resolved = resolve_store_sender(store_id)
    if resolved is None:
        logger.warning("No messaging provider for store %d, skipping winback", store_id)
        return
    _provider, send_fn = resolved
    members = load_members(store_id)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=config.winback_days)
    changed: dict = {}
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
            _send_winback(send_fn, phone, m.name, config.venue_name)
            m.winback_sent_at = now.isoformat()
            changed[phone] = m
            logger.info("Winback sent store=%d phone=%s", store_id, phone[-4:])
        except Exception as e:
            logger.warning("Winback failed store=%d phone=%s: %s", store_id, phone[-4:], e)
    if changed:
        # Save only the members this job touched -- re-writing the whole
        # loaded snapshot would clobber concurrent updates with stale rows.
        save_members(store_id, changed)


def run_winback_all() -> None:
    for store_id in _active_store_ids():
        try:
            run_winback_for_store(store_id)
        except Exception as e:
            logger.error("Winback error store=%d: %s", store_id, e)
