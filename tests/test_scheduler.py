"""Scheduled cron jobs: scout and reputation both poll hourly and only
actually scrape once their own cache-staleness check says so, so a staff
member's own command almost always hits a warm cache instead of waiting on
a live scrape -- and, unlike a fixed clock schedule, this stays in sync
with whenever a store was actually last scraped (manually or via cron),
never leaving a stale gap or wastefully re-scraping right after a manual
check.
"""
from unittest.mock import patch

import pytest

from tests.conftest import seed_chain, seed_store


@pytest.fixture
def store_id():
    chain_id = seed_chain("Cron Chain")
    return seed_store(chain_id, name="Cron Cafe", location="F-7, Islamabad")


# ── Scheduler wiring ──────────────────────────────────────────────────────────

def test_all_four_jobs_registered_with_expected_triggers():
    import scripts.run_server as rs
    scheduler = rs._start_scheduler()
    try:
        jobs = {j.id: str(j.trigger) for j in scheduler.get_jobs()}
    finally:
        scheduler.shutdown(wait=False)

    assert jobs["scout_5min_poll"] == "interval[0:05:00]"
    assert jobs["reputation_check_5min_poll"] == "interval[0:05:00]"
    # existing jobs must survive the addition, not get clobbered
    assert "winback_daily" in jobs
    assert "leaderboard_sunday" in jobs


def test_poll_jobs_cannot_run_concurrently_with_themselves():
    """A live scout scrape can take 7-45 minutes (confirmed live) --
    much longer than the 5-minute poll interval. APScheduler's
    max_instances defaults to 1 per job, meaning a poll firing while the
    previous invocation of the SAME job is still running is skipped
    rather than launched as a second concurrent execution. This is what
    actually makes a poll interval shorter than typical scrape duration
    safe -- confirm it's really in effect, not just assumed."""
    import scripts.run_server as rs
    scheduler = rs._start_scheduler()
    try:
        for job_id in ("scout_5min_poll", "reputation_check_5min_poll"):
            job = scheduler.get_job(job_id)
            assert job.max_instances == 1
    finally:
        scheduler.shutdown(wait=False)


# ── run_scout_all ──────────────────────────────────────────────────────────────

def test_run_scout_all_calls_run_with_scout_command_per_store(store_id):
    from app.agents.scout.pipeline import run_scout_all
    with patch("app.agents.scout.pipeline.run") as mock_run:
        run_scout_all()
    mock_run.assert_any_call("scout", store_id=store_id)


def test_run_scout_all_continues_past_a_failing_store(store_id):
    """One store's scout run failing (e.g. Apify down) must not stop the
    others from being attempted."""
    from app.agents.scout.pipeline import run_scout_all
    store_2 = seed_store(seed_chain("Cron Chain 2"), name="Cron Cafe 2")

    calls = []
    def _side_effect(command, store_id):
        calls.append(store_id)
        if store_id == store_2:
            raise RuntimeError("apify down")

    with patch("app.agents.scout.pipeline.run", side_effect=_side_effect):
        run_scout_all()  # must not raise

    assert set(calls) >= {store_id, store_2}


# ── run_reputation_check_all ──────────────────────────────────────────────────

def test_run_reputation_check_all_calls_check_per_store(store_id):
    from app.agents.reputation import run_reputation_check_all
    with patch("app.agents.reputation._check_reviews", return_value="ok") as mock_check:
        run_reputation_check_all()
    called_store_ids = [c.args[0] for c in mock_check.call_args_list]
    assert store_id in called_store_ids


def test_run_reputation_check_all_continues_past_a_failing_store(store_id):
    from app.agents.reputation import run_reputation_check_all
    store_2 = seed_store(seed_chain("Cron Chain 3"), name="Cron Cafe 3")

    calls = []
    def _side_effect(sid, name):
        calls.append(sid)
        if sid == store_2:
            raise RuntimeError("apify down")
        return "ok"

    with patch("app.agents.reputation._check_reviews", side_effect=_side_effect):
        run_reputation_check_all()  # must not raise

    assert set(calls) >= {store_id, store_2}


# ── REPUTATION_CACHE_HOURS ─────────────────────────────────────────────────────

def test_reputation_cache_window_is_8_hours():
    """The hourly poll relies on this cache check to decide whether to
    actually scrape -- the exact number doesn't need to divide evenly into
    anything anymore (that was only a concern under the old fixed-cron
    design), but it should still be a sane multi-hour window."""
    from app.agents.reputation import REPUTATION_CACHE_HOURS
    assert REPUTATION_CACHE_HOURS == 8


# ── _is_scout_fresh / run_scout_all skip-when-fresh ──────────────────────────

def test_is_scout_fresh_false_when_no_run_exists(store_id):
    from app.agents.scout.pipeline import _is_scout_fresh
    assert _is_scout_fresh(store_id) is False


def test_is_scout_fresh_true_for_a_recent_run(store_id):
    from app.agents.scout.pipeline import _is_scout_fresh
    from app.core.db import SessionLocal, ScoutRun
    from datetime import datetime

    with SessionLocal() as db:
        db.add(ScoutRun(store_id=store_id, command="scout", status="ok", finished_at=datetime.utcnow()))
        db.commit()

    assert _is_scout_fresh(store_id) is True


def test_is_scout_fresh_false_for_a_stale_run(store_id):
    from app.agents.scout.pipeline import _is_scout_fresh
    from app.core.db import SessionLocal, ScoutRun
    from datetime import datetime, timedelta

    with SessionLocal() as db:
        db.add(ScoutRun(
            store_id=store_id, command="scout", status="ok",
            finished_at=datetime.utcnow() - timedelta(hours=25),
        ))
        db.commit()

    assert _is_scout_fresh(store_id) is False


def test_run_scout_all_skips_a_store_whose_cache_is_still_fresh(store_id):
    """The core fix: a staff member's own recent "scout" run (or a previous
    poll's run) must stop the next hourly poll from wastefully re-scraping
    the same store."""
    from app.agents.scout.pipeline import run_scout_all
    from app.core.db import SessionLocal, ScoutRun
    from datetime import datetime

    with SessionLocal() as db:
        db.add(ScoutRun(store_id=store_id, command="scout", status="ok", finished_at=datetime.utcnow()))
        db.commit()

    with patch("app.agents.scout.pipeline.run") as mock_run:
        run_scout_all()

    mock_run.assert_not_called()


def test_run_scout_all_still_scrapes_a_store_whose_cache_is_stale(store_id):
    from app.agents.scout.pipeline import run_scout_all
    from app.core.db import SessionLocal, ScoutRun
    from datetime import datetime, timedelta

    with SessionLocal() as db:
        db.add(ScoutRun(
            store_id=store_id, command="scout", status="ok",
            finished_at=datetime.utcnow() - timedelta(hours=25),
        ))
        db.commit()

    with patch("app.agents.scout.pipeline.run") as mock_run:
        run_scout_all()

    mock_run.assert_any_call("scout", store_id=store_id)
