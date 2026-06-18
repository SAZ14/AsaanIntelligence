from __future__ import annotations

import os
import sys

from app.scrape.base import Scraper
from app.scrape.fixture import FixtureScraper


def get_scraper(force_fixture: bool = False) -> Scraper:
    """Return the best available scraper for the current environment.

    Prefers a live Browserbase scraper when credentials are present; otherwise
    (or on any construction error) falls back to the offline fixture scraper so
    callers never have to branch on configuration themselves.
    """
    if force_fixture:
        return FixtureScraper()

    has_creds = bool(
        os.environ.get("BROWSERBASE_API_KEY")
        and os.environ.get("BROWSERBASE_PROJECT_ID")
    )
    if not has_creds:
        return FixtureScraper()

    try:
        from app.scrape.browserbase import BrowserbaseScraper

        return BrowserbaseScraper()
    except Exception as e:  # pragma: no cover - depends on environment
        print(f"[scrape] Browserbase unavailable ({e}); using fixtures", file=sys.stderr)
        return FixtureScraper()
