"""Scheduled cron jobs: scout runs once daily, reputation checks 3x/day
(8h apart, matching REPUTATION_CACHE_HOURS), so a staff member's own
command almost always hits a warm cache instead of waiting on a live scrape.
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

    assert jobs["scout_daily"] == "cron[hour='6', minute='0']"
    assert jobs["reputation_check_8h"] == "cron[hour='6,14,22', minute='0']"
    # existing jobs must survive the addition, not get clobbered
    assert "winback_daily" in jobs
    assert "leaderboard_sunday" in jobs


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

def test_reputation_cache_window_matches_cron_cadence():
    """8h cache must match the 3x/day (~8h apart) cron cadence -- if these
    drift apart, a staff check could either miss a freshly-cached run or
    force an unnecessary live scrape right before the next cron fire."""
    from app.agents.reputation import REPUTATION_CACHE_HOURS
    assert REPUTATION_CACHE_HOURS == 8
