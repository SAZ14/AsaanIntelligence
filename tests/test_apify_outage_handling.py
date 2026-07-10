"""Apify account-level outage handling: quota-error detection, the shared
circuit breaker, and "don't overwrite a good cached report with an empty
one" behavior for both scout and reputation.

Confirmed live: Apify's "Monthly usage hard limit exceeded" error was
being silently swallowed by scout's scrapers (sources reported "ok" with
0 findings even though every underlying call failed), which let a 1-minute
cron cycle retry indefinitely (risking Apify rate-limiting/banning the
account) and, worse, overwrite a perfectly good cached scout report with
an empty "No competitor signals found" one every cycle.
"""
import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import seed_chain, seed_store, TestSession


@pytest.fixture
def store_id():
    chain_id = seed_chain("Outage Chain")
    return seed_store(chain_id, name="Outage Cafe", location="Blue Area, Islamabad")


@pytest.fixture
def fake_redis(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    import app.core.cache as cache
    monkeypatch.setattr(cache, "_client", fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(cache, "_unavailable", False)


# ── Quota-error detection ───────────────────────────────────────────────────

def test_is_quota_error_matches_the_real_apify_message():
    from app.core.apify_errors import is_quota_error
    real_msg = "Monthly usage hard limit exceeded. Please upgrade your subscription or contact support@apify.com"
    assert is_quota_error(real_msg) is True
    assert is_quota_error(RuntimeError(real_msg)) is True


def test_is_quota_error_does_not_match_routine_failures():
    from app.core.apify_errors import is_quota_error
    assert is_quota_error("Connection timed out") is False
    assert is_quota_error(RuntimeError("actor run failed: page not found")) is False


# ── Circuit breaker ──────────────────────────────────────────────────────────

def test_breaker_closed_initially(fake_redis):
    from app.core import apify_guard
    assert apify_guard.breaker_open() is False


def test_breaker_trips_after_three_consecutive_failures(fake_redis):
    from app.core import apify_guard
    apify_guard.record_quota_failure("scout")
    assert apify_guard.breaker_open() is False
    apify_guard.record_quota_failure("scout")
    assert apify_guard.breaker_open() is False
    apify_guard.record_quota_failure("scout")
    assert apify_guard.breaker_open() is True


def test_breaker_is_shared_between_scout_and_reputation(fake_redis):
    """They hit the same Apify account/quota -- 2 failures from reputation
    plus 1 from scout must trip the SAME shared breaker."""
    from app.core import apify_guard
    apify_guard.record_quota_failure("reputation")
    apify_guard.record_quota_failure("reputation")
    apify_guard.record_quota_failure("scout")
    assert apify_guard.breaker_open() is True


def test_success_resets_the_streak(fake_redis):
    from app.core import apify_guard
    apify_guard.record_quota_failure("scout")
    apify_guard.record_quota_failure("scout")
    apify_guard.record_success()
    apify_guard.record_quota_failure("scout")
    assert apify_guard.breaker_open() is False  # only 1 in the streak since reset


# ── review_sources.pipeline.run_pipeline: quota detection ──────────────────

def test_run_pipeline_detects_quota_exceeded(monkeypatch):
    from app.review_sources import pipeline

    monkeypatch.setattr(pipeline, "_load_config_from_db", lambda store_id: {
        "apify_api_key": "fake-key",
        "google_maps_terms": ["Outage Cafe"],
        "google_maps_location": "Islamabad",
        "instagram_usernames": ["outagecafe"],
        "store_name": "Outage Cafe",
    })

    def _quota_exceeded(*a, **kw):
        raise RuntimeError("Monthly usage hard limit exceeded. Please upgrade your subscription")

    monkeypatch.setattr(pipeline, "fetch_maps", _quota_exceeded)
    monkeypatch.setattr(pipeline, "fetch_instagram", _quota_exceeded)

    reviews, ok, failed, quota_exceeded = pipeline.run_pipeline(5)
    assert reviews == []
    assert set(failed) == {"google_maps", "instagram"}
    assert quota_exceeded is True


# ── scout.pipeline.run(): preserve previous report on quota outage ─────────

def _seed_scout_report(store_id, command, report_text):
    from app.core.db import ScoutRun, ScoutReport
    with TestSession() as db:
        run = ScoutRun(store_id=store_id, command=command, status="ok")
        db.add(run)
        db.commit()
        db.refresh(run)
        db.add(ScoutReport(store_id=store_id, run_id=run.id, command=command, report_text=report_text))
        db.commit()
        return run.id


def test_run_keeps_previous_report_on_total_quota_outage(store_id, fake_redis):
    from app.agents.scout.pipeline import run

    _seed_scout_report(store_id, "scout", "GOOD OLD REPORT: Burger Lab launched a new item.")

    with (
        patch("app.agents.scout.pipeline.confirm_seed_competitors"),
        patch("app.agents.scout.pipeline.discover_new_competitors"),
        patch("app.agents.scout.pipeline.prune_stale_competitors", return_value=0),
        patch("app.agents.scout.pipeline.get_all_competitors", return_value=[]),
        patch("app.agents.scout.pipeline._select_competitors_to_scrape", return_value=[]),
        patch("app.agents.scout.pipeline._fetch_all_sources", return_value=([], [], ["web", "instagram", "google_reviews"], True)),
        patch("app.agents.scout.pipeline.build_report") as mock_build_report,
    ):
        reply = run("scout", store_id=store_id)

    # The old report must be what's served, not a freshly-built empty one.
    assert "GOOD OLD REPORT" in reply
    assert "usage limit" in reply.lower()
    mock_build_report.assert_not_called()

    # And no new Report row should have been saved over the good one.
    from app.core.db import ScoutReport
    with TestSession() as db:
        count = db.query(ScoutReport).filter(ScoutReport.store_id == store_id).count()
    assert count == 1


def test_run_reports_no_data_when_quota_outage_and_no_prior_report(store_id, fake_redis):
    from app.agents.scout.pipeline import run

    with (
        patch("app.agents.scout.pipeline.confirm_seed_competitors"),
        patch("app.agents.scout.pipeline.discover_new_competitors"),
        patch("app.agents.scout.pipeline.prune_stale_competitors", return_value=0),
        patch("app.agents.scout.pipeline.get_all_competitors", return_value=[]),
        patch("app.agents.scout.pipeline._select_competitors_to_scrape", return_value=[]),
        patch("app.agents.scout.pipeline._fetch_all_sources", return_value=([], [], ["web", "instagram", "google_reviews"], True)),
        patch("app.agents.scout.pipeline.build_report") as mock_build_report,
    ):
        reply = run("scout", store_id=store_id)

    assert "usage limit" in reply.lower()
    mock_build_report.assert_not_called()


def test_run_scout_all_skips_every_store_when_breaker_open(store_id, fake_redis):
    from app.core import apify_guard
    from app.agents.scout.pipeline import run_scout_all

    apify_guard.record_quota_failure("scout")
    apify_guard.record_quota_failure("scout")
    apify_guard.record_quota_failure("scout")
    assert apify_guard.breaker_open() is True

    with patch("app.agents.scout.pipeline.run") as mock_run:
        run_scout_all()

    mock_run.assert_not_called()


# ── reputation._check_reviews: honest message + no fake "up to date" ───────

def test_check_reviews_gives_honest_message_on_quota_outage(store_id, fake_redis):
    from app.agents.reputation import _check_reviews, _get_reputation_last_check

    with patch("app.review_sources.pipeline.run_pipeline", return_value=([], [], ["google_maps", "instagram"], True)):
        reply = _check_reviews(store_id, "Outage Cafe")

    assert "usage limit" in reply.lower()
    assert "no new reviews" not in reply.lower()  # must not read as a normal, successful check
    assert _get_reputation_last_check(store_id) is None  # cache untouched, nothing to report as "checked"


def test_check_reviews_serves_stale_cache_when_breaker_open_never_calls_apify(store_id, fake_redis):
    """Confirmed live this needs to apply to a manual "check" command too,
    not just the scheduled cron: with the breaker open, a stale (beyond
    REPUTATION_CACHE_HOURS) but real prior check must still be served
    instead of attempting (and failing) another live scrape."""
    from datetime import datetime, timedelta
    from app.core.db import ScoutRun
    from app.core import apify_guard
    from app.agents.reputation import _check_reviews, REPUTATION_CACHE_HOURS

    with TestSession() as db:
        db.add(ScoutRun(
            store_id=store_id, command="whatsapp_check", status="ok",
            finished_at=datetime.utcnow() - timedelta(hours=REPUTATION_CACHE_HOURS + 5),
        ))
        db.commit()

    apify_guard.record_quota_failure("reputation")
    apify_guard.record_quota_failure("reputation")
    apify_guard.record_quota_failure("reputation")
    assert apify_guard.breaker_open() is True

    with patch("app.review_sources.pipeline.run_pipeline") as mock_pipeline:
        reply = _check_reviews(store_id, "Outage Cafe")

    mock_pipeline.assert_not_called()
    assert "rate-limited" in reply.lower()
    assert "780 min ago" in reply.lower()  # reports the real (stale) age, not "just now"


def test_check_reviews_reports_no_data_when_breaker_open_and_nothing_on_record(store_id, fake_redis):
    from app.core import apify_guard
    from app.agents.reputation import _check_reviews

    apify_guard.record_quota_failure("reputation")
    apify_guard.record_quota_failure("reputation")
    apify_guard.record_quota_failure("reputation")
    assert apify_guard.breaker_open() is True

    with patch("app.review_sources.pipeline.run_pipeline") as mock_pipeline:
        reply = _check_reviews(store_id, "Outage Cafe")

    mock_pipeline.assert_not_called()
    assert "usage limit" in reply.lower()


def test_run_reputation_check_all_skips_every_store_when_breaker_open(store_id, fake_redis):
    from app.core import apify_guard
    from app.agents.reputation import run_reputation_check_all

    apify_guard.record_quota_failure("reputation")
    apify_guard.record_quota_failure("reputation")
    apify_guard.record_quota_failure("reputation")
    assert apify_guard.breaker_open() is True

    with patch("app.agents.reputation._check_reviews") as mock_check:
        run_reputation_check_all()

    mock_check.assert_not_called()


# ── Fail fast: don't retry a guaranteed-fail quota error ────────────────────
# Confirmed live: tenacity's @retry decorators around each Apify call used
# retry_if_exception_type(Exception) -- retrying a quota error 2-3x (with
# exponential backoff delay on top) before the caller even got a chance to
# detect and stop it, multiplying wasted calls during an outage.

QUOTA_MSG = "Monthly usage hard limit exceeded. Please upgrade your subscription"


def test_should_retry_apify_call_predicate():
    from app.core.apify_errors import should_retry_apify_call
    assert should_retry_apify_call(RuntimeError(QUOTA_MSG)) is False
    assert should_retry_apify_call(RuntimeError("Connection reset")) is True


def test_google_reviews_run_actor_does_not_retry_quota_error():
    from app.agents.scout.scrapers.google_reviews_scraper import _run_actor
    from app.core.apify_errors import ApifyQuotaExceeded

    mock_client = MagicMock()
    mock_client.actor.return_value.call.side_effect = RuntimeError(QUOTA_MSG)
    with patch("apify_client.ApifyClient", return_value=mock_client) as mock_ctor:
        with pytest.raises((RuntimeError, ApifyQuotaExceeded)):
            _run_actor("https://maps.example/search", "mostRelevant", 10)

    assert mock_client.actor.return_value.call.call_count == 1  # no retries


def test_instagram_run_actor_does_not_retry_quota_error():
    from app.agents.scout.scrapers.instagram_scraper import _run_actor

    mock_client = MagicMock()
    mock_client.actor.return_value.call.side_effect = RuntimeError(QUOTA_MSG)
    with patch("apify_client.ApifyClient", return_value=mock_client):
        with pytest.raises(RuntimeError):
            _run_actor(["testcafe"], [], 12)

    assert mock_client.actor.return_value.call.call_count == 1  # no retries


def test_web_search_actor_does_not_retry_quota_error():
    from app.agents.scout.scrapers.web_scraper import _run_search_actor

    mock_client = MagicMock()
    mock_client.actor.return_value.call.side_effect = RuntimeError(QUOTA_MSG)
    with patch("apify_client.ApifyClient", return_value=mock_client):
        with pytest.raises(RuntimeError):
            _run_search_actor("Test Cafe Islamabad offers", 3)

    assert mock_client.actor.return_value.call.call_count == 1  # no retries


def test_web_scraper_still_retries_routine_failures():
    """Confirming the fix didn't accidentally kill retries altogether --
    a routine (non-quota) failure should still get its normal 2 attempts."""
    from app.agents.scout.scrapers.web_scraper import _run_search_actor

    mock_client = MagicMock()
    mock_client.actor.return_value.call.side_effect = RuntimeError("temporary network blip")
    with patch("apify_client.ApifyClient", return_value=mock_client):
        with pytest.raises(RuntimeError):
            _run_search_actor("Test Cafe Islamabad offers", 3)

    assert mock_client.actor.return_value.call.call_count == 2  # stop_after_attempt(2)


# ── Orphaned run reaper ──────────────────────────────────────────────────────
# Confirmed live: a deploy landing mid-live-scrape leaves the process's
# Redis lock held for its full TTL (up to 60 min for scout) with nothing
# actually running -- every request in that window wrongly reports
# "already running, please wait" instead of serving cache or retrying.

def _seed_running_run(store_id, command, started_at):
    from app.core.db import ScoutRun
    with TestSession() as db:
        run = ScoutRun(store_id=store_id, command=command, status="running", started_at=started_at)
        db.add(run)
        db.commit()
        db.refresh(run)
        return run.id


def test_reap_clears_scout_run_stuck_past_its_ttl(store_id, fake_redis):
    from datetime import datetime, timedelta
    from app.core import cache as _cache
    from app.agents.scout.config import RUN_IN_FLIGHT_MINUTES
    from app.agents.scout.pipeline import scout_live_lock_key
    from app.core.run_reaper import reap_orphaned_runs
    from app.core.db import ScoutRun

    _cache.try_lock(scout_live_lock_key(store_id), ttl_seconds=RUN_IN_FLIGHT_MINUTES * 60)
    run_id = _seed_running_run(store_id, "scout", datetime.utcnow() - timedelta(minutes=RUN_IN_FLIGHT_MINUTES + 5))

    reaped = reap_orphaned_runs()

    assert reaped == 1
    with TestSession() as db:
        run = db.query(ScoutRun).filter(ScoutRun.id == run_id).first()
        assert run.status == "error"
    assert _cache.try_lock(scout_live_lock_key(store_id), ttl_seconds=60) is True  # lock released


def test_reap_leaves_a_genuinely_recent_running_run_alone(store_id, fake_redis):
    from datetime import datetime, timedelta
    from app.core.run_reaper import reap_orphaned_runs
    from app.core.db import ScoutRun

    run_id = _seed_running_run(store_id, "scout", datetime.utcnow() - timedelta(minutes=5))

    reaped = reap_orphaned_runs()

    assert reaped == 0
    with TestSession() as db:
        run = db.query(ScoutRun).filter(ScoutRun.id == run_id).first()
        assert run.status == "running"  # untouched -- still plausibly in flight


def test_reap_clears_stuck_reputation_run(store_id, fake_redis):
    from datetime import datetime, timedelta
    from app.core import cache as _cache
    from app.agents.reputation import REPUTATION_RUN_LOCK_MINUTES, _reputation_live_lock_key
    from app.core.run_reaper import reap_orphaned_runs
    from app.core.db import ScoutRun

    _cache.try_lock(_reputation_live_lock_key(store_id), ttl_seconds=REPUTATION_RUN_LOCK_MINUTES * 60)
    run_id = _seed_running_run(
        store_id, "whatsapp_check",
        datetime.utcnow() - timedelta(minutes=REPUTATION_RUN_LOCK_MINUTES + 5),
    )

    reaped = reap_orphaned_runs()

    assert reaped == 1
    with TestSession() as db:
        run = db.query(ScoutRun).filter(ScoutRun.id == run_id).first()
        assert run.status == "error"
    assert _cache.try_lock(_reputation_live_lock_key(store_id), ttl_seconds=60) is True
