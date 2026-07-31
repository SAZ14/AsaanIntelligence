"""Conformance tests freezing the scraper/store ↔ core interface.

These prove the offline implementations honour the contracts in
`app/scrape/base.py` and `app/storage/base.py`. When Browserbase/Supabase are
wired in later, run the SAME `assert_*_conforms` helpers against them — passing
guarantees a drop-in with zero changes to `app/analysis/competitive.py`.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.models.competitive import (
    Competitor,
    CompetitorMenuItem,
    CompetitorSnapshot,
    ReviewStanding,
    Scope,
)
from app.scrape.base import Scraper, ScrapeTarget
from app.scrape.contract import assert_scraper_conforms, snapshot_violations
from app.scrape.fixture import FixtureScraper
from app.storage.base import SnapshotStore
from app.storage.contract import assert_store_conforms
from app.storage.jsonstore import JsonFileStore


# ── Protocol membership (structural typing) ──

def test_fixture_scraper_satisfies_protocol():
    assert isinstance(FixtureScraper(), Scraper)


def test_jsonfilestore_satisfies_protocol(tmp_path):
    assert isinstance(JsonFileStore(tmp_path), SnapshotStore)


def test_browserbase_scraper_class_satisfies_protocol():
    # Importing must not require creds; the class structurally conforms even
    # though constructing it would raise without BROWSERBASE_* env vars.
    from app.scrape.browserbase import BrowserbaseScraper

    assert hasattr(BrowserbaseScraper, "scrape")


def test_supabase_store_class_satisfies_protocol():
    from app.storage.supabase import SupabaseStore

    assert hasattr(SupabaseStore, "save")
    assert hasattr(SupabaseStore, "latest_before")


# ── Scraper conformance ──

@pytest.mark.parametrize("scope", [Scope.LOCAL, Scope.NATIONAL, Scope.INTERNATIONAL])
def test_fixture_scraper_conforms(scope):
    targets = [ScrapeTarget(name="*", area="F-7", city="Islamabad")]
    snaps = assert_scraper_conforms(FixtureScraper(), targets, scope)
    assert isinstance(snaps, list)


def test_snapshot_violations_flags_bad_shape():
    # A non-snapshot must be reported, not silently accepted.
    assert snapshot_violations({"not": "a snapshot"})


def test_browserbase_extracted_output_conforms():
    # The live scraper's pure JSON->model mapping must yield the same shape.
    from app.scrape.base import ScrapeTarget as T
    from app.scrape.browserbase import _snapshot_from_extracted

    snap = _snapshot_from_extracted(
        T(name="Test Cafe", area="F-7", city="Islamabad"),
        {
            "menu": [{"name": "Latte", "category": "Coffee", "price": "700", "tags": ["viral"]}],
            "promotions": [{"title": "BOGO", "discount_pct": 50}],
            "reviews": [{"source": "Google", "rating_avg": 4.5, "review_count": 120}],
            "opened_at": None,
        },
    )
    assert snapshot_violations(snap) == []


def test_hand_built_snapshot_conforms():
    snap = CompetitorSnapshot(
        competitor=Competitor(competitor_id="x", name="X", area="F-7"),
        captured_at=datetime(2026, 6, 16, 9, 0),
        menu=[CompetitorMenuItem(name="Latte", category="Coffee", price=500, tags=["a"])],
        reviews=[ReviewStanding(source="Google", rating_avg=4.5, review_count=10)],
    )
    assert snapshot_violations(snap) == []


# ── Store conformance ──

def test_jsonfilestore_conforms(tmp_path):
    # Fresh, empty store per call — a new subdirectory each time.
    counter = {"n": 0}

    def make_store():
        counter["n"] += 1
        d = tmp_path / f"store_{counter['n']}"
        return JsonFileStore(d)

    assert_store_conforms(make_store)
