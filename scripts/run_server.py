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
