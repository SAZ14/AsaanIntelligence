"""Redis cache client — thin wrapper with graceful no-op fallback.

If Redis is unavailable (misconfigured, network issue) every operation
silently degrades to a cache miss so the app keeps working via DB.
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

_client = None
_unavailable = False  # once we know Redis is down, stop retrying


def _get_redis():
    global _client, _unavailable
    if _unavailable:
        return None
    if _client is not None:
        return _client
    try:
        import redis as _redis
        r = _redis.Redis(
            host=os.getenv("REDIS_HOST", "redis.railway.internal"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        r.ping()
        _client = r
        logger.info("cache: Redis connected at %s:%s", os.getenv("REDIS_HOST"), os.getenv("REDIS_PORT"))
    except Exception as exc:
        logger.warning("cache: Redis unavailable (%s) — running without cache", exc)
        _unavailable = True
    return _client


def get(key: str):
    r = _get_redis()
    if not r:
        return None
    try:
        raw = r.get(key)
        return json.loads(raw) if raw is not None else None
    except Exception:
        return None


def set(key: str, value, ttl: int = 60) -> None:
    r = _get_redis()
    if not r:
        return
    try:
        r.setex(key, ttl, json.dumps(value, default=str))
    except Exception:
        pass


def delete(*keys: str) -> None:
    r = _get_redis()
    if not r:
        return
    try:
        r.delete(*keys)
    except Exception:
        pass


def delete_pattern(pattern: str) -> None:
    r = _get_redis()
    if not r:
        return
    try:
        keys = r.keys(pattern)
        if keys:
            r.delete(*keys)
    except Exception:
        pass


# ── Cross-instance locks / rate limits ───────────────────────────────────────
#
# These back the gateway's rate-limit / idempotency / cooldown / in-flight
# guards. They used to be plain in-process dicts (app/gateway/main.py), which
# only work correctly on a single instance -- a second Railway instance (or
# a restart mid-window) would start with empty state and silently bypass
# the guarantees those guards exist to enforce (duplicate sends, two scout
# runs racing for the same store, etc). Same fail-open philosophy as the
# rest of this module: if Redis is down, guards become permissive rather
# than blocking real traffic.

def available() -> bool:
    """True if Redis is reachable. Callers with a single-instance in-process
    fallback can use this to decide which guard to trust."""
    return _get_redis() is not None


def try_lock(key: str, ttl_seconds: int) -> bool:
    """Atomically acquire a lock. True = acquired (caller should proceed),
    False = already held by someone else. Fails open (returns True) if
    Redis is unavailable."""
    r = _get_redis()
    if not r:
        return True
    try:
        return bool(r.set(key, "1", nx=True, ex=ttl_seconds))
    except Exception:
        return True


def is_locked(key: str) -> bool:
    """Check-only (does not acquire). Fails open (returns False, i.e. 'not
    locked') if Redis is unavailable."""
    r = _get_redis()
    if not r:
        return False
    try:
        return bool(r.exists(key))
    except Exception:
        return False


def release_lock(key: str) -> None:
    delete(key)


def rate_limit_ok(key: str, max_count: int, window_seconds: int) -> bool:
    """Sliding-window rate limit via a Redis sorted set (score = arrival
    time). Returns True (and records a hit) if the caller is still under
    the limit. Fails open if Redis is unavailable."""
    r = _get_redis()
    if not r:
        return True
    try:
        import time as _time
        import uuid as _uuid
        now = _time.time()
        pipe = r.pipeline()
        pipe.zremrangebyscore(key, 0, now - window_seconds)
        pipe.zcard(key)
        _, count = pipe.execute()
        if count >= max_count:
            return False
        r.zadd(key, {f"{now}:{_uuid.uuid4().hex[:8]}": now})
        r.expire(key, window_seconds)
        return True
    except Exception:
        return True
