#!/usr/bin/env python
"""Entry point — starts the central agent server with APScheduler jobs."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# Ensure repo root is on sys.path so `import app` works when the script is
# invoked directly (e.g. `python scripts/run_server.py` from any directory).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from app.agents.customer.jobs.winback import run_winback_all
from app.agents.customer.jobs.leaderboard_broadcast import broadcast_all
from app.agents.scout.pipeline import run_scout_all
from app.agents.reputation import run_reputation_check_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Asia/Karachi")
    # Winback: daily at 10:00 AM
    scheduler.add_job(
        run_winback_all,
        trigger="cron", hour=10, minute=0,
        id="winback_daily", replace_existing=True,
    )
    # Leaderboard broadcast: every Sunday at 18:00
    scheduler.add_job(
        broadcast_all,
        trigger="cron", day_of_week="sun", hour=18, minute=0,
        id="leaderboard_sunday", replace_existing=True,
    )
    # Scout & reputation: poll hourly rather than on a fixed clock schedule.
    # Both target functions check actual cache staleness themselves
    # (run_scout_all via _is_scout_fresh, run_reputation_check_all via
    # _check_reviews' own cache check) and no-op cheaply (a DB query, no
    # Apify cost) when the cache is still warm -- so this fires every hour
    # but only actually scrapes once a store's cache has genuinely gone
    # stale. A fixed 3x/day cron drifted out of sync with real usage: e.g.
    # a staff member's manual check at 14:16 left the 8h cache fresh until
    # 22:16, but the fixed 22:00 slot landed 16 minutes early (wasted, cache
    # hit) and the next slot wasn't until 06:00 the next day -- an 8h+ gap
    # where the cache sat stale with nothing refreshing it. Polling hourly
    # and checking real staleness syncs the next scrape to "last real
    # scrape + freshness window" regardless of who triggered that scrape or
    # what the clock says.
    scheduler.add_job(
        run_scout_all,
        trigger="interval", hours=1,
        id="scout_hourly_poll", replace_existing=True,
    )
    scheduler.add_job(
        run_reputation_check_all,
        trigger="interval", hours=1,
        id="reputation_check_hourly_poll", replace_existing=True,
    )
    scheduler.start()
    return scheduler


if __name__ == "__main__":
    scheduler = _start_scheduler()
    logger.info("Scheduler started")
    dev_mode = os.environ.get("DEV", "false").lower() == "true"
    # Every DB/Redis call in the request path is synchronous (SQLAlchemy sync
    # engine, redis-py sync client) executed inline inside `async def` webhook
    # handlers -- on a single worker that means one process's single event
    # loop serializes ALL of it. A 50-concurrent-request live test confirmed
    # this: webhook ack times climbed to 4+ seconds and Railway's proxy 502'd
    # the tail of the burst. Multiple worker processes give real OS-level
    # parallelism for that blocking work. Safe to run >1 worker now that
    # cross-instance state (rate limits/cooldowns/idempotency, the job queue)
    # is Redis-backed rather than in-process dicts.
    workers = int(os.environ.get("WEB_CONCURRENCY", "4"))
    uvicorn.run(
        "app.gateway.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        reload=dev_mode,
        workers=1 if dev_mode else workers,
    )
