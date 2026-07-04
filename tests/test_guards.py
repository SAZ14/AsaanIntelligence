"""Gateway rate-limit / idempotency / cooldown / in-flight guards.

These guards are Redis-backed (cross-instance safe) with an in-process
fallback when Redis is unreachable. Both paths must enforce identical
semantics.
"""
import pytest


@pytest.fixture
def gateway_no_redis(monkeypatch):
    """Gateway guards with Redis marked unavailable -> in-process fallback."""
    import app.core.cache as cache
    from app.gateway import main as m
    monkeypatch.setattr(cache, "_client", None)
    monkeypatch.setattr(cache, "_unavailable", True)
    # isolate in-process state between tests
    m._seen_idem.clear()
    m._customer_last.clear()
    m._customer_inflight.clear()
    m._staff_last.clear()
    m._staff_inflight.clear()
    m._scout_rate.clear()
    return m


@pytest.fixture
def gateway_fake_redis(monkeypatch):
    """Gateway guards backed by a fresh fakeredis instance."""
    fakeredis = pytest.importorskip("fakeredis")
    import app.core.cache as cache
    from app.gateway import main as m
    monkeypatch.setattr(cache, "_client", fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(cache, "_unavailable", False)
    return m


def _exercise_guards(m):
    # idempotency: first delivery passes, duplicate is dropped
    assert m._idem_ok("k1") is True
    assert m._idem_ok("k1") is False

    # customer: dispatch ok, then cooldown blocks a rapid retry
    ok, reason = m._customer_dispatch_ok("p1")
    assert ok and reason is None
    ok, reason = m._customer_dispatch_ok("p1")
    assert not ok and reason == "cooldown"

    # customer: in-flight lock blocks, and clearing it releases the phone
    m._mark_inflight("customer", "p2")
    ok, reason = m._customer_dispatch_ok("p2")
    assert not ok and reason == "inflight"
    m._clear_inflight("customer", "p2")
    ok, reason = m._customer_dispatch_ok("p2")
    assert ok  # never dispatched, so no cooldown either

    # staff: dispatch ok, then cooldown
    assert m._staff_dispatch_ok("s1") is True
    assert m._staff_dispatch_ok("s1") is False

    # staff: in-flight lock
    m._mark_inflight("staff", "s2")
    assert m._staff_dispatch_ok("s2") is False
    m._clear_inflight("staff", "s2")

    # scout rate limit: 3 per window, 4th blocked
    for _ in range(3):
        assert m._scout_rate_ok("r1") is True
    assert m._scout_rate_ok("r1") is False


def test_guards_in_process_fallback(gateway_no_redis):
    _exercise_guards(gateway_no_redis)


def test_guards_redis_backed(gateway_fake_redis):
    _exercise_guards(gateway_fake_redis)
