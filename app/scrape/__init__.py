"""Scraping adapters for the competitive-intelligence agent.

`get_scraper()` returns a live Browserbase-backed scraper when credentials are
present in the environment, and otherwise falls back to the offline fixture
scraper so the pipeline stays runnable and testable without network access.
"""

from app.scrape.base import Scraper, ScrapeTarget
from app.scrape.fixture import FixtureScraper
from app.scrape.factory import get_scraper
from app.scrape.contract import assert_scraper_conforms, snapshot_violations

__all__ = [
    "Scraper", "ScrapeTarget", "FixtureScraper", "get_scraper",
    "assert_scraper_conforms", "snapshot_violations",
]
