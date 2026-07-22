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
from app.core.run_reaper import reap_orphaned_runs
from app.agents.customer.community.store import sync_chat_sessions_to_postgres

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _start_scheduler() -> BackgroundScheduler:
    # Reap once immediately on boot -- this is exactly when a deploy-
    # orphaned run (previous deployment's process killed mid-scrape,
    # never reached its own lock-release) would otherwise sit stuck for
    # up to its full TTL. Also scheduled periodically below to catch
    # crashes/OOM kills that happen without a redeploy.
    try:
        reap_orphaned_runs()
    except Exception as exc:
        logger.error("run_reaper: startup reap failed: %s", exc)

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
    # Scout & reputation: poll every 60 seconds rather than on a fixed
    # clock schedule. Both target functions check actual cache staleness
    # themselves (run_scout_all via _is_scout_fresh, run_reputation_check_all
    # via _check_reviews' own cache check, both Redis-backed and cheap on a
    # hit) and no-op with zero Apify/LLM cost when the cache is still warm --
    # so this fires every 60s but only actually scrapes once a store's
    # cache has genuinely gone stale. A fixed 3x/day cron drifted out of
    # sync with real usage: e.g. a staff member's manual check at 14:16 left
    # the 8h cache fresh until 22:16, but the fixed 22:00 slot landed 16
    # minutes early (wasted, cache hit) and the next slot wasn't until 06:00
    # the next day -- an 8h+ gap where the cache sat stale with nothing
    # refreshing it. Polling narrows that gap to roughly one poll interval
    # after the cache actually went stale, at negligible added cost either
    # way: the poll itself is just a cheap Redis/DB staleness check, the
    # expensive work (Apify/LLM) only fires on a genuine miss and is gated
    # by the freshness window itself, not by how often this poll runs.
    #
    # A live scout scrape (7-45 min, confirmed live) is far longer than
    # this interval, so overlap protection matters here more than it would
    # at a coarser cadence. Two layers: APScheduler's default max_instances
    # =1 per job means run_scout_all can never overlap with itself (a poll
    # firing mid-scrape is simply skipped, not run concurrently); and
    # separately, run() and _check_reviews() each hold their own atomic
    # Redis lock (SET NX -- app/agents/scout/pipeline.py's
    # scout_live_lock_key, app/agents/reputation.py's
    # _reputation_live_lock_key) for the duration of a live fetch, checked
    # by every caller regardless of what triggered it -- this cron poll, a
    # staff message that arrives while this poll's own scrape is still
    # running, or two staff messages arriving close together. That lock is
    # what actually prevents two concurrent live fetches for the same
    # store; the scheduler's max_instances only protects this job from
    # itself, not from a completely different caller invoking run() or
    # _check_reviews() directly.
    scheduler.add_job(
        run_scout_all,
        trigger="interval", seconds=60,
        id="scout_60s_poll", replace_existing=True,
    )
    scheduler.add_job(
        run_reputation_check_all,
        trigger="interval", seconds=60,
        id="reputation_check_60s_poll", replace_existing=True,
    )
    # Catches orphaned runs from crashes/OOM kills that happen without a
    # redeploy (the startup call above only covers the deploy case).
    scheduler.add_job(
        reap_orphaned_runs,
        trigger="interval", minutes=10,
        id="run_reaper_10min_poll", replace_existing=True,
    )
    # Batch-flush chat sessions (customer + staff) from Redis to Postgres --
    # see app/agents/customer/community/store.py's module comment for why
    # this moved off the per-turn write path.
    scheduler.add_job(
        sync_chat_sessions_to_postgres,
        trigger="interval", hours=1,
        id="chat_session_sync_hourly", replace_existing=True,
    )
    scheduler.start()
    return scheduler


if __name__ == "__main__":
    # Create any missing tables ONCE, before workers fork -- not per-worker
    # in app/gateway/main.py's lifespan() (still called there too, as a
    # safety net for anything started outside this script). Confirmed
    # live: 4 workers all calling Base.metadata.create_all() concurrently
    # the first time a brand-new table appeared raced Postgres's own
    # catalog ("duplicate key value violates unique constraint
    # pg_type_typname_nsp_index") and crashed the entire boot -- uvicorn's
    # multi-worker supervisor treats any one worker failing to start as
    # fatal and tears down every already-started worker with it, not just
    # the one that failed.
    from app.core.db import init_db
    init_db()

    scheduler = _start_scheduler()
    logger.info("Scheduler started")
    dev_mode = os.environ.get("DEV", "false").lower() == "true"
    # Every DB/Redis call in the request path is synchronous (SQLAlchemy sync
    # engine, redis-py sync client). It used to run inline inside `async def`
    # webhook handlers, so on a single worker one process's single event loop
    # serialized ALL of it -- a 50-concurrent-request live test confirmed
    # this: webhook ack times climbed to 4+ seconds and Railway's proxy 502'd
    # the tail of the burst. That inline work now runs via run_in_threadpool
    # (app/gateway/main.py's openwa_webhook/meta_webhook) so it no longer
    # blocks the event loop directly, but multiple worker processes still
    # give real OS-level parallelism on top of that -- each is a separate
    # process with its own event loop, thread pool and DB connection pool.
    # Kept at 4, NOT raised: confirmed live that 8 workers starting
    # simultaneously (each spinning up an embedding-model warmup thread, a
    # job-queue heartbeat thread, and PyTorch/tokenizers' own internal
    # thread pools) hits the container's OS thread limit at startup --
    # "RuntimeError: can't start new thread" / "Resource temporarily
    # unavailable", which crashed every worker and took the whole service
    # down. The vCPU/RAM headroom on this host doesn't raise that ceiling.
    # Safe to run >1 worker since cross-instance state (rate limits/
    # cooldowns/idempotency, the job queue) is Redis-backed rather than
    # in-process dicts.
    workers = int(os.environ.get("WEB_CONCURRENCY", "4"))
    uvicorn.run(
        "app.gateway.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        reload=dev_mode,
        workers=1 if dev_mode else workers,
    )
