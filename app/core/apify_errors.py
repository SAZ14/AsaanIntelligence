"""Shared detection for Apify account-level failures (e.g. "Monthly usage
hard limit exceeded"), distinct from routine per-item scraping failures (a
single competitor's page 404s, a malformed response). Both scout's
scrapers and reputation's review_sources pipeline raise ApifyQuotaExceeded
the moment they see this pattern instead of silently swallowing it and
retrying every remaining item -- confirmed live: once the account hits its
monthly limit, every subsequent Apify call that run (and every call on
every following cron cycle) is also guaranteed to fail, so scout was
burning through ~12 competitors x 2-3 Apify calls each per cycle, and
reputation's cron was repeating the same guaranteed-fail calls every
single minute.
"""
from __future__ import annotations


class ApifyQuotaExceeded(Exception):
    """Raised when an Apify call fails with an account-level quota/limit
    error rather than a routine per-item failure."""


def is_quota_error(exc: BaseException | str) -> bool:
    msg = str(exc).lower()
    return "usage hard limit" in msg or ("apify.com" in msg and "upgrade" in msg)


def should_retry_apify_call(exc: BaseException) -> bool:
    """Predicate for tenacity's retry_if_exception -- retry on routine
    per-item failures (a timeout, a transient 500), but NOT on a quota
    error, since every retry is then a guaranteed-fail call by
    definition. Confirmed live: scout's scrapers were retrying a quota
    error up to 3x each (with exponential backoff delay on top) before
    the caller even got a chance to detect it and stop, multiplying
    wasted Apify calls during an outage instead of failing fast."""
    return not is_quota_error(exc)
