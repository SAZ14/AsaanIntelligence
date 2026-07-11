"""Chat session storage (app.agents.customer.community.store): Redis is now
the hot-path source of truth for both customer and staff chat history,
batch-synced to Postgres hourly instead of written on every single turn
(sync_chat_sessions_to_postgres, wired into scripts/run_server.py's
scheduler). Falls back to writing Postgres directly when Redis itself is
unavailable, so a session isn't silently lost for an entire outage.
"""
import pytest

from tests.conftest import seed_chain, seed_store, TestSession


@pytest.fixture
def store_id():
    chain_id = seed_chain("Sync Chain")
    return seed_store(chain_id, name="Sync Cafe", location="G-9, Islamabad")


@pytest.fixture
def fake_redis(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    import app.core.cache as cache
    monkeypatch.setattr(cache, "_client", fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(cache, "_unavailable", False)


PHONE = "+923001234567"


# ── cache.py primitives ──────────────────────────────────────────────────────

def test_sadd_and_smembers_round_trip(fake_redis):
    from app.core import cache as _cache
    _cache.sadd("test:set", "a")
    _cache.sadd("test:set", "b")
    assert _cache.smembers("test:set") == {"a", "b"}


def test_smembers_empty_without_redis():
    from app.core import cache as _cache
    assert _cache.smembers("nonexistent") == set()


def test_rename_for_batch_moves_and_returns_true(fake_redis):
    from app.core import cache as _cache
    _cache.sadd("src", "x")
    assert _cache.rename_for_batch("src", "dst") is True
    assert _cache.smembers("dst") == {"x"}
    assert _cache.smembers("src") == set()


def test_rename_for_batch_false_when_source_missing(fake_redis):
    from app.core import cache as _cache
    assert _cache.rename_for_batch("does-not-exist", "dst") is False


# ── save_chat_session: Redis-primary, no immediate Postgres write ──────────

def test_save_marks_dirty_and_does_not_write_postgres_immediately(store_id, fake_redis):
    from app.agents.customer.community.store import save_chat_session, OrmChatSession
    from app.core import cache as _cache

    save_chat_session(store_id, PHONE, [{"role": "user", "content": "hi"}])

    with TestSession() as db:
        row = db.query(OrmChatSession).filter(
            OrmChatSession.store_id == store_id, OrmChatSession.phone == PHONE,
        ).first()
    assert row is None  # not yet synced to Postgres

    assert f"{store_id}:{PHONE}" in _cache.smembers("chat_sessions:dirty")


def test_load_still_works_from_redis_before_sync(store_id, fake_redis):
    from app.agents.customer.community.store import save_chat_session, load_chat_session
    save_chat_session(store_id, PHONE, [{"role": "user", "content": "hi"}])
    assert load_chat_session(store_id, PHONE) == [{"role": "user", "content": "hi"}]


def test_save_falls_back_to_postgres_when_redis_unavailable(store_id):
    """No fake_redis fixture here -- Redis is genuinely unavailable, same
    as this repo's local/test environment. A session must not be silently
    lost for the whole outage."""
    from app.agents.customer.community.store import save_chat_session, OrmChatSession
    save_chat_session(store_id, PHONE, [{"role": "user", "content": "hi"}])

    with TestSession() as db:
        row = db.query(OrmChatSession).filter(
            OrmChatSession.store_id == store_id, OrmChatSession.phone == PHONE,
        ).first()
    assert row is not None
    assert row.history == [{"role": "user", "content": "hi"}]


# ── sync_chat_sessions_to_postgres ──────────────────────────────────────────

def test_sync_flushes_dirty_sessions_to_postgres(store_id, fake_redis):
    from app.agents.customer.community.store import (
        save_chat_session, sync_chat_sessions_to_postgres, OrmChatSession,
    )
    save_chat_session(store_id, PHONE, [{"role": "user", "content": "hi"}])
    save_chat_session(store_id, "+923009999999", [{"role": "user", "content": "hello"}])

    synced = sync_chat_sessions_to_postgres()
    assert synced == 2

    with TestSession() as db:
        rows = db.query(OrmChatSession).filter(OrmChatSession.store_id == store_id).all()
    assert len(rows) == 2


def test_sync_updates_an_existing_row(store_id, fake_redis):
    from app.agents.customer.community.store import save_chat_session, sync_chat_sessions_to_postgres, OrmChatSession

    save_chat_session(store_id, PHONE, [{"role": "user", "content": "first"}])
    sync_chat_sessions_to_postgres()

    save_chat_session(store_id, PHONE, [{"role": "user", "content": "first"}, {"role": "assistant", "content": "reply"}])
    synced = sync_chat_sessions_to_postgres()
    assert synced == 1

    with TestSession() as db:
        rows = db.query(OrmChatSession).filter(
            OrmChatSession.store_id == store_id, OrmChatSession.phone == PHONE,
        ).all()
    assert len(rows) == 1  # updated in place, not duplicated
    assert len(rows[0].history) == 2


def test_sync_clears_the_dirty_set(store_id, fake_redis):
    from app.agents.customer.community.store import save_chat_session, sync_chat_sessions_to_postgres
    from app.core import cache as _cache

    save_chat_session(store_id, PHONE, [{"role": "user", "content": "hi"}])
    sync_chat_sessions_to_postgres()
    assert _cache.smembers("chat_sessions:dirty") == set()


def test_sync_is_a_noop_when_nothing_dirty(fake_redis):
    from app.agents.customer.community.store import sync_chat_sessions_to_postgres
    assert sync_chat_sessions_to_postgres() == 0


def test_staff_and_customer_sessions_both_sync_correctly(store_id, fake_redis):
    """The dirty-set member format ("store_id:phone") must split correctly
    even when phone itself contains a colon -- staff mode's namespaced key
    ("staff:whatsapp:+92...", gateway/internal.py) is exactly that case."""
    from app.agents.customer.community.store import save_chat_session, sync_chat_sessions_to_postgres, OrmChatSession

    staff_key = "staff:whatsapp:+923001234567"
    save_chat_session(store_id, staff_key, [{"role": "user", "content": "leakage"}])
    save_chat_session(store_id, PHONE, [{"role": "user", "content": "hi"}])

    synced = sync_chat_sessions_to_postgres()
    assert synced == 2

    with TestSession() as db:
        staff_row = db.query(OrmChatSession).filter(
            OrmChatSession.store_id == store_id, OrmChatSession.phone == staff_key,
        ).first()
        customer_row = db.query(OrmChatSession).filter(
            OrmChatSession.store_id == store_id, OrmChatSession.phone == PHONE,
        ).first()
    assert staff_row.history == [{"role": "user", "content": "leakage"}]
    assert customer_row.history == [{"role": "user", "content": "hi"}]


def test_writes_during_sync_are_not_lost(store_id, fake_redis):
    """A save that lands after the dirty set has been renamed off for
    batching must still get picked up by the NEXT sync -- verified here
    by saving again right after the rename step a real sync would do."""
    from app.agents.customer.community.store import save_chat_session, sync_chat_sessions_to_postgres, _DIRTY_SESSIONS_KEY
    from app.core import cache as _cache

    save_chat_session(store_id, PHONE, [{"role": "user", "content": "first"}])
    # Simulate the rename step of a sync starting...
    _cache.rename_for_batch(_DIRTY_SESSIONS_KEY, "manual-swap")
    # ...then a new write arrives before that batch finishes.
    save_chat_session(store_id, PHONE, [{"role": "user", "content": "first"}, {"role": "user", "content": "second"}])
    assert f"{store_id}:{PHONE}" in _cache.smembers(_DIRTY_SESSIONS_KEY)

    _cache.delete("manual-swap")
    synced = sync_chat_sessions_to_postgres()
    assert synced == 1  # the second write's dirty mark still gets synced
