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
