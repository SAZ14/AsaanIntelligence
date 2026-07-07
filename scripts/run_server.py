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
    # Scout: once daily, early -- so it's already sitting in the 24h
    # freshness cache by the time staff check during business hours,
    # instead of them waiting 7-45 minutes for a live run.
    scheduler.add_job(
        run_scout_all,
        trigger="cron", hour=6, minute=0,
        id="scout_daily", replace_existing=True,
    )
    # Reputation check: 3x/day, 8h apart, matching REPUTATION_CACHE_HOURS --
    # a staff "check" almost always lands on a warm cache instead of
    # waiting on a live Apify scrape.
    scheduler.add_job(
        run_reputation_check_all,
        trigger="cron", hour="6,14,22", minute=0,
        id="reputation_check_8h", replace_existing=True,
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
