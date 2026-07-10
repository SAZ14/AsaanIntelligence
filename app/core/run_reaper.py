"""Reap scout/reputation runs orphaned by a process that died mid-scrape
(a deploy's SIGTERM, a crash, an OOM kill) before it could reach its own
`finally: release_lock(...)`.

Confirmed live: a deploy landing mid-live-scrape left the Redis in-flight
lock held for its full TTL (up to RUN_IN_FLIGHT_MINUTES = 60 min for
scout) with nothing actually running -- every "scout"/"check" request in
that window wrongly reported "already running, please wait" instead of
serving cache or starting a fresh attempt. This used to require a manual
SSH session to release the lock by hand.

Any Run row still marked status="running" past its command's own max
plausible duration can only mean the process that owned it died --
run()/`_check_reviews()` always transition out of "running" (to ok/
partial/error) before returning on every normal path, success or
failure. Reaping is safe to run redundantly (idempotent, cheap) from
multiple worker processes or on every scheduler tick.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def reap_orphaned_runs() -> int:
    """Find and clear orphaned runs. Returns how many were reaped."""
    from app.core.db import SessionLocal, Run
    from app.core import cache as _cache
    from app.agents.scout.config import RUN_IN_FLIGHT_MINUTES
    from app.agents.scout.pipeline import scout_live_lock_key
    from app.agents.reputation import REPUTATION_RUN_LOCK_MINUTES, _reputation_live_lock_key

    reaped = 0
    with SessionLocal() as db:
        stuck = db.query(Run).filter(Run.status == "running").all()
        now = datetime.utcnow()
        for run in stuck:
            if run.started_at is None:
                continue
            is_reputation = run.command == "whatsapp_check"
            max_minutes = REPUTATION_RUN_LOCK_MINUTES if is_reputation else RUN_IN_FLIGHT_MINUTES
            if run.started_at >= now - timedelta(minutes=max_minutes):
                continue  # still within its plausible in-flight window -- leave it alone

            lock_key = (
                _reputation_live_lock_key(run.store_id) if is_reputation
                else scout_live_lock_key(run.store_id)
            )
            logger.warning(
                "run_reaper: store=%d run_id=%d command=%s stuck in 'running' since %s "
                "(> %d min) -- marking error and releasing lock",
                run.store_id, run.id, run.command, run.started_at, max_minutes,
            )
            run.status = "error"
            run.finished_at = now
            _cache.release_lock(lock_key)
            reaped += 1
        if reaped:
            db.commit()
    return reaped
