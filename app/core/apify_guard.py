"""Shared circuit breaker for Apify account-level failures, used by both
scout's and reputation's scheduled cron jobs (run_scout_all,
run_reputation_check_all) -- they hit the same Apify account/quota, so a
failure surfaced by one job is a failure for both.

Confirmed live: a "Monthly usage hard limit exceeded" error means every
subsequent Apify call is ALSO guaranteed to fail until the account is
upgraded or the quota resets next month. Letting each 1-minute cron cycle
keep retrying regardless risks Apify rate-limiting or banning the account
outright. This trips after 3 consecutive quota failures (the count is
shared across both jobs), after which BOTH jobs skip every Apify attempt
for a cooldown window instead of retrying forever. Deliberately scoped to
the scheduled cron polls only -- a staff member's own explicit "scout" or
"check" command is user-paced (not a tight automated loop) and is left
free to try regardless of breaker state.
"""
from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

_FAILURE_COUNT_KEY = "apify:consecutive_failures"
_BREAKER_KEY = "apify:breaker_open"
FAILURE_THRESHOLD = 3
BREAKER_COOLDOWN_HOURS = 24
# A failure this old no longer counts toward the consecutive streak -- an
# isolated quota error from hours ago shouldn't combine with a fresh one to
# trip the breaker after just 2 genuinely-consecutive failures.
_STREAK_WINDOW_SECONDS = 3600


def breaker_open() -> bool:
    from app.core import cache as _cache
    return _cache.get(_BREAKER_KEY) is not None


def record_success() -> None:
    from app.core import cache as _cache
    _cache.delete(_FAILURE_COUNT_KEY, _BREAKER_KEY)


def record_quota_failure(source: str) -> None:
    """source is a label ("scout"/"reputation") for the log line only --
    the streak and breaker are shared across both."""
    from app.core import cache as _cache
    count = (_cache.get(_FAILURE_COUNT_KEY) or 0) + 1
    _cache.set(_FAILURE_COUNT_KEY, count, ttl=_STREAK_WINDOW_SECONDS)
    if count >= FAILURE_THRESHOLD:
        _cache.set(_BREAKER_KEY, {"tripped_by": source}, ttl=BREAKER_COOLDOWN_HOURS * 3600)
        logger.error(
            "apify_guard: breaker OPEN after %d consecutive Apify quota failures "
            "(tripped_by=%s) -- pausing automated polling for %dh",
            count, source, BREAKER_COOLDOWN_HOURS,
        )
    else:
        logger.warning(
            "apify_guard: consecutive_failures=%d/%d (source=%s)",
            count, FAILURE_THRESHOLD, source,
        )
