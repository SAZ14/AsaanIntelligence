"""Durable job queue: enqueue/process, retries, restart-orphan recovery,
and the BackgroundTasks fallback contract when Redis is unavailable.
"""
import json
import threading
import time

import pytest
from unittest.mock import patch

from app.core import jobqueue


@pytest.fixture
def fake_redis(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    r = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(jobqueue, "_queue_client", r)
    monkeypatch.setattr(jobqueue, "_queue_unavailable", False)
    return r


@pytest.fixture
def no_redis(monkeypatch):
    monkeypatch.setattr(jobqueue, "_queue_client", None)
    monkeypatch.setattr(jobqueue, "_queue_unavailable", True)


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


def test_long_running_job_does_not_get_wrongly_orphaned(fake_redis, monkeypatch):
    """Reproduces the live incident: a job that runs longer than
    HEARTBEAT_TTL must NOT have its instance declared dead and its job
    resurrected by another instance's reaper mid-execution.

    Old bug: _heartbeat() only ran between process_one() calls, so a single
    slow job blocked the refresh for its whole duration. Fix: heartbeat runs
    on its own thread, independent of job execution.
    """
    monkeypatch.setattr(jobqueue, "HEARTBEAT_TTL", 1)  # shrink for a fast test
    started = threading.Event()
    finish = threading.Event()
    calls = []

    def slow_handler(job, send_fn):
        calls.append(job["id"])
        started.set()
        finish.wait(timeout=5)  # blocks process_one() well past HEARTBEAT_TTL

    jobqueue.register_handler("scout", slow_handler)
    jobqueue.enqueue("scout", 5, "+92300", "scout", TWILIO_REPLY_TO)

    worker = threading.Thread(target=jobqueue.process_one, kwargs={"timeout": 2}, daemon=True)
    worker.start()
    assert started.wait(timeout=2), "handler never started"

    # Heartbeat thread keeps the instance's liveness key fresh WHILE the
    # handler is still blocking process_one() -- this is the exact window
    # where the old code let the key expire.
    hb = threading.Thread(target=jobqueue._heartbeat_loop, daemon=True)
    hb_stop_backup = jobqueue._stop
    jobqueue._stop.clear()
    monkeypatch.setattr(jobqueue, "HEARTBEAT_INTERVAL", 0.2)
    hb.start()
    time.sleep(1.5)  # well past the 1s TTL if nothing were refreshing it

    assert jobqueue.reap_orphans() == 0, "job was wrongly resurrected while its instance was still alive"

    finish.set()
    worker.join(timeout=5)
    jobqueue._stop.set()
    hb.join(timeout=2)
    jobqueue._stop = hb_stop_backup
    assert calls == [calls[0]], "handler ran more than once for a single job"


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
