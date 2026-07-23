"""Weekly leaderboard broadcast — runs for all stores."""
from __future__ import annotations

import logging

from app.agents.customer.community.leaderboard import format_leaderboard, weekly_stamp_counts
from app.agents.customer.community.store import load_members, load_venue_config

logger = logging.getLogger(__name__)


def _active_store_ids() -> list[int]:
    from app.core.db import SessionLocal, Store
    from app.core.entitlements import filter_entitled
    with SessionLocal() as db:
        store_ids = [s.id for s in db.query(Store).all()]
    return filter_entitled(store_ids, "customer")


def broadcast_for_store(store_id: int) -> int:
    from app.core.outbound import resolve_store_sender
    resolved = resolve_store_sender(store_id)
    if resolved is None:
        logger.warning("No messaging provider for store %d, skipping broadcast", store_id)
        return 0
    _provider, send_fn = resolved
    members = load_members(store_id)
    counts = weekly_stamp_counts(store_id)
    config = load_venue_config(store_id)
    message = format_leaderboard(
        counts, members,
        title=f"{config.venue_name}, this week's top collectors",
    )
    sent = 0
    for member in members.values():
        if not member.opted_in:
            continue
        try:
            send_fn(member.phone, message)
            sent += 1
        except Exception as e:
            logger.warning("Broadcast failed store=%d phone=...%s: %s", store_id, member.phone[-4:], e)
    return sent


def broadcast_all() -> None:
    for store_id in _active_store_ids():
        try:
            n = broadcast_for_store(store_id)
            logger.info("Leaderboard broadcast store=%d sent=%d", store_id, n)
        except Exception as e:
            logger.error("Broadcast error store=%d: %s", store_id, e)
