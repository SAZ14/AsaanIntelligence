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
