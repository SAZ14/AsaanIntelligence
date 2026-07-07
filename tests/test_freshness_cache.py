"""Scout/reputation freshness caching: Redis fast path (same connection as
the rest of this project's rate limits/cooldowns/job queue) in front of
Postgres, which remains the source of truth. A Redis hit skips the Postgres
query entirely; a miss (cold start, restart, key eviction, or Redis simply
being down -- cache.py fails open the same way everywhere else in this
codebase) falls back to the original Postgres check and repopulates Redis.
"""
from datetime import datetime, timedelta

import pytest

from tests.conftest import seed_chain, seed_store, TestSession


@pytest.fixture
def store_id():
    chain_id = seed_chain("Freshness Chain")
    return seed_store(chain_id, name="Freshness Cafe", location="F-7, Islamabad")


@pytest.fixture
def fake_redis(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    import app.core.cache as cache
    monkeypatch.setattr(cache, "_client", fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(cache, "_unavailable", False)
    return cache


@pytest.fixture
def no_redis(monkeypatch):
    """Redis marked unavailable -> every check must fall back to Postgres
    and keep working (fail-open, matching the rest of this codebase)."""
    import app.core.cache as cache
    monkeypatch.setattr(cache, "_client", None)
    monkeypatch.setattr(cache, "_unavailable", True)
    return cache


def _seed_run(store_id, command, status="ok", finished_at=None):
    from app.core.db import ScoutRun
    with TestSession() as db:
        db.add(ScoutRun(
            store_id=store_id, command=command, status=status,
            finished_at=finished_at or datetime.utcnow(),
        ))
        db.commit()


# ── Scout: _is_scout_fresh ───────────────────────────────────────────────────

class TestScoutFreshnessCache:
    def test_redis_hit_returns_fresh_without_touching_postgres(self, store_id, fake_redis):
        from app.agents.scout.pipeline import _mark_scout_run_fresh, _is_scout_fresh
        _mark_scout_run_fresh(store_id, datetime.utcnow(), freshness_minutes=1440)
        # No ScoutRun row exists in Postgres at all -- if this returns True,
        # it can only have come from Redis.
        assert _is_scout_fresh(store_id) is True

    def test_redis_miss_falls_back_to_postgres(self, store_id, fake_redis):
        from app.agents.scout.pipeline import _is_scout_fresh
        _seed_run(store_id, "scout", status="ok")
        assert _is_scout_fresh(store_id) is True

    def test_redis_miss_and_postgres_stale_is_not_fresh(self, store_id, fake_redis):
        from app.agents.scout.pipeline import _is_scout_fresh
        _seed_run(store_id, "scout", status="ok", finished_at=datetime.utcnow() - timedelta(hours=25))
        assert _is_scout_fresh(store_id) is False

    def test_postgres_fallback_repopulates_redis(self, store_id, fake_redis):
        """After a Redis miss that Postgres confirms as fresh, the next
        check should hit Redis directly."""
        from app.agents.scout.pipeline import _is_scout_fresh, _scout_cache_key
        from app.core import cache as _cache
        _seed_run(store_id, "scout", status="ok")
        assert _cache.get(_scout_cache_key(store_id)) is None
        _is_scout_fresh(store_id)
        assert _cache.get(_scout_cache_key(store_id)) is not None

    def test_works_without_redis_at_all(self, store_id, no_redis):
        """Fail-open: no Redis configured must not break freshness checks,
        only skip the fast path."""
        from app.agents.scout.pipeline import _is_scout_fresh
        _seed_run(store_id, "scout", status="ok")
        assert _is_scout_fresh(store_id) is True

    def test_run_completion_marks_redis_fresh(self, store_id, fake_redis):
        from app.agents.scout.pipeline import run, _scout_cache_key
        from app.core import cache as _cache
        from unittest.mock import patch

        with patch("app.agents.scout.pipeline.confirm_seed_competitors"), \
             patch("app.agents.scout.pipeline.discover_new_competitors"), \
             patch("app.agents.scout.pipeline.prune_stale_competitors", return_value=0), \
             patch("app.agents.scout.pipeline.get_all_competitors", return_value=[]), \
             patch("app.agents.scout.pipeline._select_competitors_to_scrape", return_value=[]), \
             patch("app.agents.scout.pipeline._fetch_all_sources", return_value=([], ["web"], [])), \
             patch("app.agents.scout.pipeline.build_report", return_value="report text"):
            run("scout", store_id=store_id)

        assert _cache.get(_scout_cache_key(store_id)) is not None


# ── Reputation: _get_reputation_last_check ───────────────────────────────────

class TestReputationFreshnessCache:
    def test_redis_hit_returns_timestamp_without_touching_postgres(self, store_id, fake_redis):
        from app.agents.reputation import _mark_reputation_checked, _get_reputation_last_check
        now = datetime.utcnow()
        _mark_reputation_checked(store_id, now)
        result = _get_reputation_last_check(store_id)
        assert result is not None
        assert abs((result - now).total_seconds()) < 1

    def test_redis_miss_falls_back_to_postgres(self, store_id, fake_redis):
        from app.agents.reputation import _get_reputation_last_check
        _seed_run(store_id, "whatsapp_check", status="ok")
        assert _get_reputation_last_check(store_id) is not None

    def test_redis_miss_and_postgres_stale_returns_none(self, store_id, fake_redis):
        from app.agents.reputation import _get_reputation_last_check
        _seed_run(store_id, "whatsapp_check", status="ok",
                   finished_at=datetime.utcnow() - timedelta(hours=9))
        assert _get_reputation_last_check(store_id) is None

    def test_only_ok_status_counts_not_partial(self, store_id, fake_redis):
        """Matches the pre-existing Postgres query's filter exactly --
        REPUTATION_CACHE_HOURS freshness only ever counted status == "ok"."""
        from app.agents.reputation import _get_reputation_last_check
        _seed_run(store_id, "whatsapp_check", status="partial")
        assert _get_reputation_last_check(store_id) is None

    def test_postgres_fallback_repopulates_redis(self, store_id, fake_redis):
        from app.agents.reputation import _get_reputation_last_check, _reputation_cache_key
        from app.core import cache as _cache
        _seed_run(store_id, "whatsapp_check", status="ok")
        assert _cache.get(_reputation_cache_key(store_id)) is None
        _get_reputation_last_check(store_id)
        assert _cache.get(_reputation_cache_key(store_id)) is not None

    def test_works_without_redis_at_all(self, store_id, no_redis):
        from app.agents.reputation import _get_reputation_last_check
        _seed_run(store_id, "whatsapp_check", status="ok")
        assert _get_reputation_last_check(store_id) is not None

    def test_check_reputation_cache_uses_redis_hit(self, store_id, fake_redis):
        from app.agents.reputation import _mark_reputation_checked, check_reputation_cache
        _mark_reputation_checked(store_id, datetime.utcnow())
        # No pending finding, no ScoutRun row in Postgres at all -- if this
        # reports a cache hit, it can only have come from Redis.
        ok, text = check_reputation_cache(store_id, "Freshness Cafe")
        assert ok is True
        assert "up to date" in text.lower() or "pending" in text.lower()
