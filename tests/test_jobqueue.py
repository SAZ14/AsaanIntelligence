"""Durable job queue: enqueue/process, retries, restart-orphan recovery,
and the BackgroundTasks fallback contract when Redis is unavailable.
"""
import json

import pytest
from unittest.mock import patch

from app.core import jobqueue


@pytest.fixture
def fake_redis(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    import app.core.cache as cache
    r = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(cache, "_client", r)
    monkeypatch.setattr(cache, "_unavailable", False)
    return r


@pytest.fixture
def no_redis(monkeypatch):
    import app.core.cache as cache
    monkeypatch.setattr(cache, "_client", None)
    monkeypatch.setattr(cache, "_unavailable", True)


@pytest.fixture(autouse=True)
def clean_handlers():
    saved = dict(jobqueue._handlers)
    yield
    jobqueue._handlers.clear()
    jobqueue._handlers.update(saved)


TWILIO_REPLY_TO = {"provider": "twilio", "to": "whatsapp:+92300", "from_": "whatsapp:+92111"}


def test_enqueue_returns_false_without_redis(no_redis):
    assert jobqueue.enqueue("scout", 1, "+92300", "scout", TWILIO_REPLY_TO) is False


def test_enqueue_and_process_one(fake_redis):
    calls = []

    def handler(job, send_fn):
        calls.append(job)
        send_fn("the report")

    jobqueue.register_handler("scout", handler)
    assert jobqueue.enqueue("scout", 5, "+92300", "scout please", TWILIO_REPLY_TO) is True
    assert fake_redis.llen(jobqueue.PENDING_KEY) == 1

    with patch("app.core.twilio_send.send_whatsapp") as mock_send:
        assert jobqueue.process_one(timeout=1) is True

    assert len(calls) == 1
    assert calls[0]["store_id"] == 5
    assert calls[0]["body"] == "scout please"
    mock_send.assert_called_once_with(
        to="whatsapp:+92300", body="the report", from_="whatsapp:+92111"
    )
    # nothing left anywhere
    assert fake_redis.llen(jobqueue.PENDING_KEY) == 0
    assert fake_redis.llen(jobqueue.PROCESSING_PREFIX + jobqueue.INSTANCE_ID) == 0


def test_openwa_reply_to_builds_openwa_sender(fake_redis):
    jobqueue.register_handler("internal", lambda job, send_fn: send_fn("done"))
    jobqueue.enqueue("internal", 5, "+92300", "summary",
                     {"provider": "openwa", "session_id": "sess-1", "jid": "92300@c.us"})
    with patch("app.core.openwa_send.send_openwa") as mock_send:
        jobqueue.process_one(timeout=1)
    mock_send.assert_called_once_with("sess-1", "92300@c.us", "done")


def test_failed_job_is_retried_then_apologises(fake_redis):
    attempts = []

    def failing(job, send_fn):
        attempts.append(job["attempts"])
        raise RuntimeError("boom")

    jobqueue.register_handler("scout", failing)
    jobqueue.enqueue("scout", 5, "+92300", "scout", TWILIO_REPLY_TO)

    with patch("app.core.twilio_send.send_whatsapp") as mock_send:
        for _ in range(jobqueue.MAX_ATTEMPTS):
            assert jobqueue.process_one(timeout=1) is True
        # queue is drained now
        assert jobqueue.process_one(timeout=1) is False

    assert attempts == [0, 1, 2]
    # only the final attempt notifies the user
    mock_send.assert_called_once()
    assert "try again" in mock_send.call_args.kwargs["body"].lower()


def test_reap_orphans_requeues_dead_instances_jobs(fake_redis):
    orphan = json.dumps({"id": "j1", "kind": "scout", "store_id": 5,
                         "from_number": "+92300", "body": "scout",
                         "ack": None, "reply_to": TWILIO_REPLY_TO, "attempts": 0})
    fake_redis.lpush(jobqueue.PROCESSING_PREFIX + "deadbeef", orphan)
    # no jobs:alive:deadbeef heartbeat -> instance counts as dead
    assert jobqueue.reap_orphans() == 1
    assert fake_redis.llen(jobqueue.PENDING_KEY) == 1
    assert fake_redis.llen(jobqueue.PROCESSING_PREFIX + "deadbeef") == 0


def test_reap_skips_live_instances(fake_redis):
    orphan = json.dumps({"id": "j2", "kind": "scout", "store_id": 5,
                         "from_number": "+92300", "body": "scout",
                         "ack": None, "reply_to": TWILIO_REPLY_TO, "attempts": 0})
    fake_redis.lpush(jobqueue.PROCESSING_PREFIX + "livebeef", orphan)
    fake_redis.set(jobqueue.ALIVE_PREFIX + "livebeef", "1", ex=60)
    assert jobqueue.reap_orphans() == 0
    assert fake_redis.llen(jobqueue.PROCESSING_PREFIX + "livebeef") == 1


def test_gateway_dispatch_falls_back_to_background_tasks(no_redis):
    """Without Redis, _dispatch_durable must schedule the old BackgroundTask."""
    from app.gateway.main import _dispatch_durable, _bg_scout
    from fastapi import BackgroundTasks

    bt = BackgroundTasks()
    sent = []
    _dispatch_durable(bt, "scout", 5, "+92300", "scout",
                      TWILIO_REPLY_TO, _bg_scout, sent.append)
    assert len(bt.tasks) == 1
    assert bt.tasks[0].func is _bg_scout


def test_gateway_dispatch_enqueues_with_redis(fake_redis):
    from app.gateway.main import _dispatch_durable, _bg_internal
    from fastapi import BackgroundTasks

    bt = BackgroundTasks()
    _dispatch_durable(bt, "internal", 5, "+92300", "summary",
                      TWILIO_REPLY_TO, _bg_internal, lambda s: None, ack="On it")
    assert len(bt.tasks) == 0  # queued durably, no BackgroundTask
    raw = fake_redis.lrange(jobqueue.PENDING_KEY, 0, -1)
    assert len(raw) == 1
    job = json.loads(raw[0])
    assert job["kind"] == "internal"
    assert job["ack"] == "On it"
