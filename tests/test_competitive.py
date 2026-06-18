"""Tests for the deterministic parts of the competitive-intelligence agent.

Covers pricing comparison, new-dish diffing, review momentum, menu gaps,
national-trend aggregation, and the fixture scraper / JSON store roundtrip.
No LLM calls are exercised here — only pure analysis. Tests assert general
properties rather than hardcoded answer keys.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.analysis.competitive import (
    MIN_PRICING_SAMPLE,
    active_promotions,
    area_pricing,
    detect_new_dishes,
    menu_gaps,
    momentum_map,
    national_trends,
    review_trends,
)
from app.models.canonical import MenuItem
from app.models.competitive import (
    Competitor,
    CompetitorMenuItem,
    CompetitorPromotion,
    CompetitorSnapshot,
    ReviewStanding,
    Scope,
)
from app.scrape.fixture import FixtureScraper, load_snapshots_file
from app.storage.jsonstore import JsonFileStore


# ── Fixtures ──

def _menu_item(name, cat, price) -> MenuItem:
    return MenuItem(sku=name[:3].upper(), name=name, category=cat, price=price)


def _home_menu() -> dict[str, MenuItem]:
    return {
        "ESP": _menu_item("Espresso", "Coffee", 400),
        "LAT": _menu_item("Latte", "Coffee", 500),
        "CAP": _menu_item("Cappuccino", "Coffee", 600),
    }


def _snap(cid, name, area, menu, reviews, captured, *, opened=None, promos=None,
          country="Pakistan", city="Islamabad") -> CompetitorSnapshot:
    return CompetitorSnapshot(
        competitor=Competitor(
            competitor_id=cid, name=name, area=area, city=city,
            country=country, opened_at=opened,
        ),
        captured_at=captured,
        menu=[CompetitorMenuItem(**m) for m in menu],
        promotions=[CompetitorPromotion(**p) for p in (promos or [])],
        reviews=[ReviewStanding(**r) for r in reviews],
    )


NOW = datetime(2026, 6, 16, 9, 0)
PREV = datetime(2026, 5, 16, 9, 0)


# ── Pricing vs area average ──

class TestAreaPricing:
    def test_below_average_recommends_raise(self):
        rivals = [_snap("c1", "A", "F-7", [
            {"name": "x", "category": "Coffee", "price": 800},
            {"name": "y", "category": "Coffee", "price": 820},
            {"name": "z", "category": "Coffee", "price": 840},
        ], [], NOW)]
        insights = area_pricing(_home_menu(), rivals)
        coffee = [i for i in insights if i.category == "coffee"]
        assert len(coffee) == 1
        assert coffee[0].recommendation == "raise"
        assert coffee[0].pct_diff < 0

    def test_above_average_flags_premium_risk(self):
        rivals = [_snap("c1", "A", "F-7", [
            {"name": "x", "category": "Coffee", "price": 300},
            {"name": "y", "category": "Coffee", "price": 320},
            {"name": "z", "category": "Coffee", "price": 340},
        ], [], NOW)]
        insights = area_pricing(_home_menu(), rivals)
        coffee = [i for i in insights if i.category == "coffee"]
        assert coffee[0].recommendation == "premium_risk"
        assert coffee[0].pct_diff > 0

    def test_insufficient_sample_is_skipped(self):
        # Fewer than MIN_PRICING_SAMPLE rival points → no recommendation.
        rivals = [_snap("c1", "A", "F-7", [
            {"name": "x", "category": "Coffee", "price": 800},
        ], [], NOW)]
        assert MIN_PRICING_SAMPLE > 1
        insights = area_pricing(_home_menu(), rivals)
        assert insights == []

    def test_category_matching_is_case_insensitive(self):
        rivals = [_snap("c1", "A", "F-7", [
            {"name": "x", "category": "COFFEE", "price": 800},
            {"name": "y", "category": "coffee", "price": 820},
            {"name": "z", "category": "Coffee", "price": 840},
        ], [], NOW)]
        insights = area_pricing(_home_menu(), rivals)
        assert any(i.category == "coffee" for i in insights)


# ── New-dish detection ──

class TestNewDishes:
    def test_dish_added_since_prev_is_flagged(self):
        prev = _snap("c1", "A", "F-7", [
            {"name": "Latte", "category": "Coffee", "price": 700},
        ], [], PREV)
        cur = _snap("c1", "A", "F-7", [
            {"name": "Latte", "category": "Coffee", "price": 700},
            {"name": "Biscoff Tres Leches", "category": "Bakery", "price": 650},
        ], [], NOW)
        alerts = detect_new_dishes([cur], {"c1": prev})
        names = [a.dish_name for a in alerts]
        assert "Biscoff Tres Leches" in names
        assert "Latte" not in names

    def test_existing_dish_not_flagged(self):
        prev = _snap("c1", "A", "F-7", [
            {"name": "Latte", "category": "Coffee", "price": 700},
        ], [], PREV)
        cur = _snap("c1", "A", "F-7", [
            {"name": "latte", "category": "Coffee", "price": 720},  # same dish, reprice
        ], [], NOW)
        alerts = detect_new_dishes([cur], {"c1": prev})
        assert alerts == []

    def test_no_previous_and_not_new_venue_is_silent(self):
        cur = _snap("c1", "A", "F-7", [
            {"name": "Latte", "category": "Coffee", "price": 700},
        ], [], NOW)
        assert detect_new_dishes([cur], {}) == []

    def test_brand_new_venue_flags_all_dishes(self):
        cur = _snap("c1", "A", "F-7", [
            {"name": "Nashville Sandwich", "category": "Food", "price": 1600},
            {"name": "Corn Dog", "category": "Food", "price": 900},
        ], [], NOW, opened=datetime.now() - timedelta(days=10))
        alerts = detect_new_dishes([cur], {})
        assert len(alerts) == 2
        assert all(a.is_new_venue for a in alerts)

    def test_momentum_is_carried_through(self):
        cur = _snap("c1", "A", "F-7", [
            {"name": "New Thing", "category": "Food", "price": 900},
        ], [], NOW, opened=datetime.now() - timedelta(days=5))
        alerts = detect_new_dishes([cur], {}, {"c1": "rising"})
        assert alerts[0].momentum == "rising"


# ── Review momentum ──

class TestReviewTrends:
    def test_review_growth_reads_as_rising(self):
        prev = _snap("c1", "A", "F-7", [], [{"source": "Google", "rating_avg": 4.5, "review_count": 100}], PREV)
        cur = _snap("c1", "A", "F-7", [], [{"source": "Google", "rating_avg": 4.5, "review_count": 200}], NOW)
        trends = review_trends([cur], {"c1": prev})
        assert trends[0].direction == "rising"
        assert trends[0].reviews_delta == 100

    def test_rating_drop_reads_as_falling(self):
        prev = _snap("c1", "A", "F-7", [], [{"source": "Google", "rating_avg": 4.6, "review_count": 100}], PREV)
        cur = _snap("c1", "A", "F-7", [], [{"source": "Google", "rating_avg": 4.0, "review_count": 105}], NOW)
        trends = review_trends([cur], {"c1": prev})
        assert trends[0].direction == "falling"

    def test_no_previous_is_steady(self):
        cur = _snap("c1", "A", "F-7", [], [{"source": "Google", "rating_avg": 4.5, "review_count": 100}], NOW)
        trends = review_trends([cur], {})
        assert trends[0].direction == "steady"
        assert trends[0].reviews_delta == 0

    def test_momentum_map_keys_by_competitor(self):
        prev = _snap("c1", "A", "F-7", [], [{"source": "Google", "rating_avg": 4.5, "review_count": 100}], PREV)
        cur = _snap("c1", "A", "F-7", [], [{"source": "Google", "rating_avg": 4.5, "review_count": 300}], NOW)
        m = momentum_map(review_trends([cur], {"c1": prev}))
        assert m["c1"] == "rising"


# ── Promotions ──

class TestPromotions:
    def test_active_promo_surfaced(self):
        cur = _snap("c1", "A", "F-7", [], [], NOW, promos=[
            {"title": "BOGO", "description": "weekend", "discount_pct": 50},
        ])
        promos = active_promotions([cur])
        assert len(promos) == 1
        assert promos[0].title == "BOGO"

    def test_expired_promo_excluded(self):
        cur = _snap("c1", "A", "F-7", [], [], NOW, promos=[
            {"title": "Old", "ends_at": datetime(2020, 1, 1)},
        ])
        assert active_promotions([cur]) == []


# ── Menu gaps ──

class TestMenuGaps:
    def test_gap_requires_multiple_rivals(self):
        rivals = [
            _snap("c1", "A", "F-7", [{"name": "Karak", "category": "Tea", "price": 250}], [], NOW),
            _snap("c2", "B", "F-6", [{"name": "Kashmiri", "category": "Tea", "price": 350}], [], NOW),
        ]
        gaps = menu_gaps(_home_menu(), rivals, min_rivals=2)
        assert any(g.category == "tea" for g in gaps)

    def test_single_rival_below_threshold(self):
        rivals = [
            _snap("c1", "A", "F-7", [{"name": "Karak", "category": "Tea", "price": 250}], [], NOW),
        ]
        gaps = menu_gaps(_home_menu(), rivals, min_rivals=2)
        assert gaps == []

    def test_category_we_carry_is_not_a_gap(self):
        rivals = [
            _snap("c1", "A", "F-7", [{"name": "Mocha", "category": "Coffee", "price": 700}], [], NOW),
            _snap("c2", "B", "F-6", [{"name": "Brew", "category": "Coffee", "price": 700}], [], NOW),
        ]
        gaps = menu_gaps(_home_menu(), rivals, min_rivals=2)
        assert not any(g.category == "coffee" for g in gaps)


# ── National trend aggregation ──

class TestNationalTrends:
    def test_tag_across_venues_is_a_trend(self):
        rivals = [
            _snap("c1", "A", "F-7", [{"name": "Spanish Latte", "category": "Coffee", "price": 700, "tags": ["viral"]}], [], NOW),
            _snap("c2", "B", "Gulberg", [{"name": "Corn Dog", "category": "Food", "price": 800, "tags": ["viral"]}], [], NOW, city="Lahore"),
        ]
        signals = national_trends(rivals, min_venues=2)
        assert any(s.label == "viral" and s.kind == "tag" for s in signals)

    def test_single_venue_tag_not_a_trend(self):
        rivals = [
            _snap("c1", "A", "F-7", [{"name": "X", "category": "Coffee", "price": 700, "tags": ["unique"]}], [], NOW),
        ]
        signals = national_trends(rivals, min_venues=2)
        assert not any(s.label == "unique" for s in signals)


# ── Fixture scraper + store roundtrip ──

class TestFixtureAndStore:
    def test_fixture_scraper_loads_snapshots(self):
        scraper = FixtureScraper()  # default bundled current fixture
        snaps = scraper.scrape([], Scope.NATIONAL)
        assert len(snaps) > 0
        assert all(isinstance(s, CompetitorSnapshot) for s in snaps)

    def test_local_scope_filters_by_area(self):
        from app.scrape.base import ScrapeTarget

        scraper = FixtureScraper()
        targets = [ScrapeTarget(name="*", area="F-7", city="Islamabad")]
        snaps = scraper.scrape(targets, Scope.LOCAL)
        # Only Islamabad-area venues, not Karachi/Lahore national-only ones.
        assert all(s.competitor.city == "Islamabad" for s in snaps)

    def test_store_roundtrip_returns_baseline(self, tmp_path):
        store = JsonFileStore(tmp_path)
        prev = _snap("c1", "A", "F-7", [{"name": "Latte", "category": "Coffee", "price": 700}],
                     [{"source": "Google", "rating_avg": 4.5, "review_count": 100}], PREV)
        store.save([prev])
        got = store.latest_before(Scope.LOCAL, NOW)
        assert "c1" in got
        assert got["c1"].competitor.name == "A"

    def test_store_excludes_snapshots_at_or_after_run(self, tmp_path):
        store = JsonFileStore(tmp_path)
        future = _snap("c1", "A", "F-7", [], [], NOW)
        store.save([future])
        # Nothing strictly before a run that starts at NOW.
        assert store.latest_before(Scope.LOCAL, NOW) == {}

    def test_browserbase_extraction_is_pure_and_validates(self):
        # The JSON->snapshot mapping must work without any SDK or network.
        from app.scrape.base import ScrapeTarget
        from app.scrape.browserbase import _snapshot_from_extracted

        target = ScrapeTarget(name="Test Cafe", area="F-7", city="Islamabad")
        data = {
            "menu": [{"name": "Latte", "category": "Coffee", "price": "700", "tags": ["viral"]}],
            "promotions": [{"title": "BOGO", "discount_pct": 50}],
            "reviews": [{"source": "Google", "rating_avg": 4.5, "review_count": 120}],
            "opened_at": None,
        }
        snap = _snapshot_from_extracted(target, data)
        assert snap.competitor.name == "Test Cafe"
        assert snap.menu[0].price == 700.0
        assert snap.total_reviews == 120

    def test_bundled_fixtures_parse(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "data" / "competitive"
        prev = load_snapshots_file(root / "snapshot_prev.json")
        cur = load_snapshots_file(root / "snapshot_current.json")
        assert len(prev) > 0 and len(cur) > 0
        # The current fixture should contain at least one brand-new venue
        # (opened within 90 days) to exercise new-venue detection.
        assert any(s.competitor.is_new for s in cur)
