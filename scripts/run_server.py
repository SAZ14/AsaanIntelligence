#!/usr/bin/env python
"""Entry point — starts the central agent server with APScheduler jobs."""
from __future__ import annotations

import logging
import os

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


def _start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Asia/Karachi")
    # Winback: daily at 10:00 AM
    scheduler.add_job(
        "app.agents.customer.jobs.winback:run_winback_all",
        trigger="cron", hour=10, minute=0,
        id="winback_daily", replace_existing=True,
    )
    # Leaderboard broadcast: every Sunday at 18:00
    scheduler.add_job(
        "app.agents.customer.jobs.leaderboard_broadcast:broadcast_all",
        trigger="cron", day_of_week="sun", hour=18, minute=0,
        id="leaderboard_sunday", replace_existing=True,
    )
    scheduler.start()
    return scheduler


if __name__ == "__main__":
    scheduler = _start_scheduler()
    logger.info("Scheduler started")
    uvicorn.run(
        "app.gateway.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        reload=os.environ.get("DEV", "false").lower() == "true",
    )
