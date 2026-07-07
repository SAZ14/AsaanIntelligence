"""Durable Redis-backed job queue for long-running staff work.

FastAPI's BackgroundTasks is fire-and-forget in-process: a Railway restart
or redeploy mid-run silently loses the job, after the user was already told
"your report will arrive in 7-10 minutes". This queue persists jobs in Redis
so they survive crashes and get retried.

Mechanics (classic reliable-queue pattern):
  - enqueue(): LPUSH a JSON job onto jobs:pending. Returns False if Redis is
    unavailable so the caller can fall back to BackgroundTasks — same
    fail-open philosophy as app.core.cache.
  - A worker thread per instance BRPOPLPUSHes pending → jobs:processing:<iid>
    (its own list), executes, then LREMs the entry when done.
  - The instance refreshes a heartbeat key (jobs:alive:<iid>, 60s TTL) while
    its worker is alive.
  - A reaper (on startup + periodically) requeues entries from any
    processing list whose instance heartbeat has expired — this is what
    recovers jobs orphaned by a restart or crash.
  - Failed jobs are retried up to MAX_ATTEMPTS; after that the user gets an
    apology message instead of silence.

Jobs are plain dicts:
  {"id", "kind", "store_id", "from_number", "body", "ack",
   "reply_to": {"provider": "twilio", "to", "from_"}
             | {"provider": "openwa", "session_id", "jid"},
   "attempts"}

Handlers are registered per kind via register_handler(); they receive
(job, send_fn) where send_fn is rebuilt from reply_to.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Callable

logger = logging.getLogger(__name__)

# The queue needs its own connection: app.core.cache's client uses
# socket_timeout=2, which kills every BRPOPLPUSH block (up to POP_TIMEOUT
# seconds) at the socket layer before it can return empty-handed.
POP_TIMEOUT = 5  # seconds a blocking pop waits for work

_queue_client = None
_queue_unavailable = False


def _get_redis():
    global _queue_client, _queue_unavailable
    if _queue_unavailable:
        return None
    if _queue_client is not None:
        return _queue_client
    try:
        import redis as _redis
        r = _redis.Redis(
            host=os.getenv("REDIS_HOST", "redis.railway.internal"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=POP_TIMEOUT + 5,  # must outlast the blocking pop
        )
        r.ping()
        _queue_client = r
    except Exception as exc:
        logger.warning("jobqueue: Redis unavailable (%s)", exc)
        _queue_unavailable = True
    return _queue_client

PENDING_KEY = "jobs:pending"
PROCESSING_PREFIX = "jobs:processing:"
ALIVE_PREFIX = "jobs:alive:"
HEARTBEAT_TTL = 60          # seconds; reaper treats a missing heartbeat as dead
REAP_INTERVAL = 60          # seconds between orphan scans
MAX_ATTEMPTS = 3

INSTANCE_ID = uuid.uuid4().hex[:12]

_handlers: dict[str, Callable] = {}
_worker_thread: threading.Thread | None = None
_stop = threading.Event()


def register_handler(kind: str, fn: Callable) -> None:
    """fn(job: dict, send_fn: Callable[[str], None]) -> None"""
    _handlers[kind] = fn


def _build_send_fn(reply_to: dict) -> Callable[[str], None]:
    provider = reply_to.get("provider")
    if provider == "twilio":
        def _send(body: str) -> None:
            from app.core.twilio_send import send_whatsapp
            send_whatsapp(to=reply_to["to"], body=body, from_=reply_to["from_"])
        return _send
    if provider == "openwa":
        def _send(body: str) -> None:
            from app.core.openwa_send import send_openwa
            send_openwa(reply_to["session_id"], reply_to["jid"], body)
        return _send
    if provider == "meta":
        def _send(body: str) -> None:
            from app.core.meta_send import send_meta
            send_meta(reply_to["phone_number_id"], reply_to["to"], body)
        return _send
    raise ValueError(f"unknown reply_to provider: {provider!r}")


def enqueue(kind: str, store_id: int, from_number: str, body: str,
            reply_to: dict, ack: str | None = None) -> bool:
    """Persist a job. False = Redis unavailable, caller must fall back."""
    r = _get_redis()
    if r is None:
        return False
    job = {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "store_id": store_id,
        "from_number": from_number,
        "body": body,
        "ack": ack,
        "reply_to": reply_to,
        "attempts": 0,
    }
    try:
        r.lpush(PENDING_KEY, json.dumps(job))
        logger.info("jobqueue.enqueue: kind=%s store=%d job=%s", kind, store_id, job["id"])
        return True
    except Exception as exc:
        logger.warning("jobqueue.enqueue: failed (%s) — caller should fall back", exc)
        return False


def _execute(raw: str) -> None:
    """Run one job; on failure requeue up to MAX_ATTEMPTS, then apologise."""
    job = json.loads(raw)
    kind = job.get("kind", "")
    handler = _handlers.get(kind)
    if handler is None:
        logger.error("jobqueue: no handler for kind=%r — dropping job %s", kind, job.get("id"))
        return
    send_fn = _build_send_fn(job["reply_to"])
    try:
        handler(job, send_fn)
        logger.info("jobqueue: done kind=%s job=%s", kind, job["id"])
    except Exception as exc:
        job["attempts"] = job.get("attempts", 0) + 1
        logger.error("jobqueue: kind=%s job=%s attempt=%d failed: %s",
                     kind, job["id"], job["attempts"], exc)
        r = _get_redis()
        if job["attempts"] < MAX_ATTEMPTS and r is not None:
            try:
                r.lpush(PENDING_KEY, json.dumps(job))
                return
            except Exception:
                pass
        try:
            send_fn("Sorry, that request couldn't be completed. Please try again.")
        except Exception:
            logger.error("jobqueue: failure notice undeliverable for job %s", job["id"])


def process_one(timeout: int = 5) -> bool:
    """Pop and run a single job. Returns True if one was processed.
    Separated from the worker loop so tests can drive the queue directly."""
    r = _get_redis()
    if r is None:
        return False
    processing_key = PROCESSING_PREFIX + INSTANCE_ID
    try:
        raw = r.brpoplpush(PENDING_KEY, processing_key, timeout=timeout)
    except Exception as exc:
        logger.warning("jobqueue: pop failed (%s)", exc)
        time.sleep(timeout)
        return False
    if raw is None:
        return False
    try:
        _execute(raw)
    finally:
        try:
            r.lrem(processing_key, 1, raw)
        except Exception:
            pass
    return True


def reap_orphans() -> int:
    """Requeue jobs stuck in processing lists of instances whose heartbeat
    is gone (crashed / redeployed mid-job). Returns number requeued."""
    r = _get_redis()
    if r is None:
        return 0
    requeued = 0
    try:
        for key in r.keys(PROCESSING_PREFIX + "*"):
            iid = key[len(PROCESSING_PREFIX):]
            if iid == INSTANCE_ID or r.exists(ALIVE_PREFIX + iid):
                continue
            while True:
                raw = r.rpoplpush(key, PENDING_KEY)
                if raw is None:
                    break
                requeued += 1
                logger.info("jobqueue.reap: requeued orphan from dead instance %s", iid)
    except Exception as exc:
        logger.warning("jobqueue.reap: failed (%s)", exc)
    return requeued


def _heartbeat() -> None:
    r = _get_redis()
    if r is None:
        return
    try:
        r.set(ALIVE_PREFIX + INSTANCE_ID, "1", ex=HEARTBEAT_TTL)
    except Exception:
        pass


# Heartbeat interval must stay well under HEARTBEAT_TTL so a slow tick or two
# never lets the key expire while the instance is genuinely alive.
HEARTBEAT_INTERVAL = 20  # seconds


def _heartbeat_loop() -> None:
    """Runs on its OWN thread, independent of job processing.

    A single scout job blocks process_one() for minutes at a time (real
    Apify scrapes + LLM analysis). The heartbeat used to be refreshed from
    inside that same loop, so once a job ran longer than HEARTBEAT_TTL, this
    instance's heartbeat key expired while it was still very much alive and
    working — another instance's reaper would then see the expired key,
    conclude this instance had crashed, and requeue the job it was still
    actively processing. A second (or third, or fourth...) instance would
    pick up the "orphan" and run the exact same job again, sending a
    duplicate ack and duplicate final report each time, indefinitely, since
    this path has no attempt cap (confirmed live: one scout request produced
    4 full duplicate runs before intervention). Decoupling the heartbeat
    from job execution is what actually fixes that — this thread ticks
    every HEARTBEAT_INTERVAL regardless of how long the worker thread is
    stuck inside a single job.
    """
    while not _stop.is_set():
        _heartbeat()
        _stop.wait(HEARTBEAT_INTERVAL)


def _reaper_loop() -> None:
    """Also independent of job processing, for the same reason as above --
    an instance stuck on a long job should still be ABLE to reap other
    instances' orphans in the meantime."""
    while not _stop.is_set():
        try:
            reap_orphans()
        except Exception as exc:
            logger.error("jobqueue: reaper iteration error: %s", exc)
        _stop.wait(REAP_INTERVAL)


def _worker_loop() -> None:
    logger.info("jobqueue: worker started instance=%s", INSTANCE_ID)
    while not _stop.is_set():
        try:
            process_one(timeout=POP_TIMEOUT)
        except Exception as exc:
            logger.error("jobqueue: worker iteration error: %s", exc)
            time.sleep(5)


_heartbeat_thread: threading.Thread | None = None
_reaper_thread: threading.Thread | None = None


def start_worker() -> None:
    """Start the background worker, heartbeat, and reaper threads (idempotent).
    No-op without Redis — the gateway falls back to BackgroundTasks in that
    case anyway."""
    global _worker_thread, _heartbeat_thread, _reaper_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    if _get_redis() is None:
        logger.info("jobqueue: Redis unavailable — worker not started (BackgroundTasks fallback active)")
        return
    _stop.clear()
    _heartbeat()  # set the key immediately so a startup-time reap doesn't self-orphan
    _heartbeat_thread = threading.Thread(target=_heartbeat_loop, name="jobqueue-heartbeat", daemon=True)
    _heartbeat_thread.start()
    _reaper_thread = threading.Thread(target=_reaper_loop, name="jobqueue-reaper", daemon=True)
    _reaper_thread.start()
    _worker_thread = threading.Thread(target=_worker_loop, name="jobqueue-worker", daemon=True)
    _worker_thread.start()


def stop_worker() -> None:
    _stop.set()
