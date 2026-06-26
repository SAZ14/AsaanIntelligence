"""Tests for the deterministic parts of the reputation agent.

Tests cover general properties — no hardcoded answer keys.
LLM calls are not tested here; only correlation + pattern detection.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.agents.reputation import (
    ReviewAnalysis,
    VisitContext,
    correlate_review,
    detect_patterns,
)
from app.models.canonical import MenuItem, Order, Review, Staff, LineItem, Payment


# ── Fixtures ──

def _staff() -> dict[str, Staff]:
    return {
        "S01": Staff(staff_id="S01", name="Ayesha", role="barista"),
        "S02": Staff(staff_id="S02", name="Hamza", role="barista"),
        "S03": Staff(staff_id="S03", name="Bilal", role="server"),
    }


def _menu() -> dict[str, MenuItem]:
    return {
        "FLAT": MenuItem(sku="FLAT", name="Flat White", category="coffee", price=500),
        "CAKE": MenuItem(sku="CAKE", name="Cheesecake", category="dessert", price=650),
        "ESP": MenuItem(sku="ESP", name="Espresso", category="coffee", price=400),
    }


def _make_order(order_id: str, dt_: datetime, staff_id: str) -> Order:
    return Order(
        order_id=order_id,
        datetime=dt_,
        staff_id=staff_id,
        staff_name="",
        channel="dine-in",
        order_status="completed",
        line_items=[LineItem(
            item_sku="FLAT", item_name="Flat White", category="coffee",
            qty=1, unit_price=500, line_amount=500,
        )],
        payments=[Payment(method="cash", amount=500, tax_rate=0.16)],
    )


def _orders_on_saturday_evening() -> list[Order]:
    """Several orders on Saturday 2026-05-16 between 20:00-21:00."""
    base = datetime(2026, 5, 16, 20, 0)
    return [
        _make_order(f"O{i}", base + timedelta(minutes=i * 10), "S03")
        for i in range(5)
    ]


# ── Staff name attribution ──

class TestStaffAttribution:
    def test_review_naming_staff_is_attributed(self):
        """A review mentioning a staff first name must be attributed to that staff."""
        review = Review(
            review_id="T01", source="Google", rating=2,
            posted_at=datetime(2026, 5, 17, 10, 0),
            reviewer_name="Test User",
            text="The barista Bilal was short with us and rang up the wrong order.",
        )
        ctx = correlate_review(review, _orders_on_saturday_evening(), _staff(), _menu())
        assert ctx.matched_staff_name == "Bilal"
        assert ctx.matched_staff_id == "S03"
        assert ctx.confidence == "high"

    def test_staff_name_case_insensitive(self):
        """Staff matching should be case-insensitive."""
        review = Review(
            review_id="T02", source="Google", rating=3,
            posted_at=datetime(2026, 5, 17, 10, 0),
            reviewer_name="Test User",
            text="AYESHA was great today.",
        )
        ctx = correlate_review(review, [], _staff(), _menu())
        assert ctx.matched_staff_name == "Ayesha"
        assert ctx.matched_staff_id == "S01"

    def test_no_false_staff_match(self):
        """Names not in staff list must not match."""
        review = Review(
            review_id="T03", source="Google", rating=5,
            posted_at=datetime(2026, 5, 17, 10, 0),
            reviewer_name="Test User",
            text="The girl at the counter was very helpful.",
        )
        ctx = correlate_review(review, [], _staff(), _menu())
        assert ctx.matched_staff_name == ""


# ── No-match guard for vague reviews ──

class TestNoMatchGuard:
    def test_vague_review_gets_no_match(self):
        """A review with no time/day/staff/item clues should be 'none' confidence."""
        review = Review(
            review_id="T10", source="Google", rating=1,
            posted_at=datetime(2026, 5, 22, 16, 0),
            reviewer_name="Hira J.",
            text="Disappointing.",
        )
        ctx = correlate_review(review, _orders_on_saturday_evening(), _staff(), _menu())
        # Even vague reviews get candidate dates (yesterday/today), so confidence
        # depends on whether any extra signal is found. With no signal at all,
        # confidence should be "none".
        assert ctx.confidence == "none"

    def test_item_mention_gives_low_confidence(self):
        """Mentioning a menu item but nothing else → low confidence."""
        review = Review(
            review_id="T11", source="Google", rating=4,
            posted_at=datetime(2026, 5, 17, 10, 0),
            reviewer_name="Test User",
            text="The flat white was really good.",
        )
        ctx = correlate_review(review, _orders_on_saturday_evening(), _staff(), _menu())
        assert ctx.confidence == "low"
        assert any("Flat White" in r for r in ctx.match_reasons)

    def test_day_mention_gives_medium_confidence(self):
        """Mentioning a day of week but no time → medium confidence."""
        review = Review(
            review_id="T12", source="Google", rating=3,
            posted_at=datetime(2026, 5, 17, 10, 0),
            reviewer_name="Test User",
            text="Came on Saturday, place was packed.",
        )
        ctx = correlate_review(review, _orders_on_saturday_evening(), _staff(), _menu())
        assert ctx.confidence == "medium"
        assert ctx.estimated_date != ""

    def test_day_plus_time_gives_high_confidence(self):
        """Mentioning day + time → high confidence."""
        review = Review(
            review_id="T13", source="Google", rating=2,
            posted_at=datetime(2026, 5, 17, 10, 0),
            reviewer_name="Test User",
            text="Saturday around 8pm was chaos, waited forever.",
        )
        ctx = correlate_review(review, _orders_on_saturday_evening(), _staff(), _menu())
        assert ctx.confidence == "high"
        assert ctx.estimated_date != ""
        assert ctx.estimated_hour_range != ""


# ── Pattern / cluster detection ──

class TestPatternDetection:
    def _make_analyses(self, issue: str, dow: str, hours: str,
                       ids: list[str], confidence: str = "high") -> list[ReviewAnalysis]:
        return [
            ReviewAnalysis(
                review_id=rid, source="Google", rating=2,
                posted_at="2026-05-17", reviewer_name="X", text="bad",
                correlation=VisitContext(
                    estimated_date=dow,
                    estimated_hour_range=hours,
                    confidence=confidence,
                ),
                issue_class=issue, sentiment="negative",
            )
            for rid in ids
        ]

    def test_service_speed_cluster_detected(self):
        """Multiple service_speed complaints in same day/hour window → cluster."""
        analyses = self._make_analyses(
            "service_speed", "2026-05-16", "19:00–21:00",
            ["R03", "R07", "R15"],
        )
        patterns = detect_patterns(analyses)
        assert len(patterns) >= 1
        speed_patterns = [p for p in patterns if p.issue == "service_speed"]
        assert len(speed_patterns) >= 1
        assert speed_patterns[0].review_count >= 2

    def test_no_cluster_from_single_review(self):
        """A single complaint should not form a pattern."""
        analyses = self._make_analyses(
            "food_quality", "2026-05-16", "12:00–14:00",
            ["R99"],
        )
        patterns = detect_patterns(analyses)
        assert len(patterns) == 0

    def test_praise_excluded_from_patterns(self):
        """Praise reviews should not appear in issue patterns."""
        analyses = self._make_analyses(
            "praise", "2026-05-16", "19:00–21:00",
            ["R50", "R51", "R52"],
        )
        patterns = detect_patterns(analyses)
        assert len(patterns) == 0

    def test_no_match_reviews_excluded(self):
        """Reviews with confidence='none' should not contribute to patterns."""
        analyses = self._make_analyses(
            "service_speed", "2026-05-16", "19:00–21:00",
            ["R60", "R61", "R62"],
            confidence="none",
        )
        patterns = detect_patterns(analyses)
        assert len(patterns) == 0

    def test_pattern_ids_match_input(self):
        """Pattern review_ids should match the input reviews."""
        ids = ["RA", "RB", "RC"]
        analyses = self._make_analyses(
            "staff_attitude", "2026-05-14", "17:00–19:00", ids,
        )
        patterns = detect_patterns(analyses)
        assert len(patterns) == 1
        assert set(patterns[0].review_ids) == set(ids)


# ── Correlation with real review data shape ──

class TestCorrelationOnDataShape:
    def test_review_with_staff_and_time(self):
        """Review mentioning staff + time → high confidence with staff ID."""
        review = Review(
            review_id="T20", source="Google", rating=2,
            posted_at=datetime(2026, 5, 15, 10, 0),
            reviewer_name="Test",
            text="Bilal was rude around 6pm.",
        )
        orders = [
            _make_order("OX1", datetime(2026, 5, 14, 18, 0), "S03"),
            _make_order("OX2", datetime(2026, 5, 14, 18, 30), "S03"),
        ]
        ctx = correlate_review(review, orders, _staff(), _menu())
        assert ctx.confidence == "high"
        assert ctx.matched_staff_name == "Bilal"
        assert ctx.order_count_in_window >= 1


def test_process_reputation_owner_reply_no_store(monkeypatch):
    sent_messages = []
    
    def mock_send(to, text, from_key=None):
        sent_messages.append((to, text))
        return "mock-sid"
        
    class MockTable:
        def select(self, *args, **kwargs):
            return self
        def eq(self, *args, **kwargs):
            return self
        def execute(self):
            class MockData:
                data = []
            return MockData()
            
    class MockSupabase:
        def table(self, table_name):
            return MockTable()
            
    monkeypatch.setattr("app.services.messaging.send_whatsapp_text", mock_send)
    
    import app.database
    monkeypatch.setattr(app.database, "supabase", MockSupabase())
    
    from app.agents.reputation import process_reputation_owner_reply
    process_reputation_owner_reply("+923001234567", "POST")
    
    assert len(sent_messages) == 1
    assert "not registered as an owner" in sent_messages[0][1]


def test_process_reputation_owner_reply_chat_fallback(monkeypatch):
    from types import SimpleNamespace
    sent_messages = []
    
    def mock_send(to, text, from_key=None):
        sent_messages.append((to, text))
        return "mock-sid"
        
    class MockTable:
        def select(self, *args, **kwargs):
            return self
        def eq(self, *args, **kwargs):
            return self
        def maybe_single(self, *args, **kwargs):
            return self
        def execute(self):
            class MockData:
                # Mock response for store_members and stores
                data = [{"store_id": 999, "name": "Sugar Rush", "id": 999}]
            return MockData()
            
    class MockSupabase:
        def table(self, table_name):
            return MockTable()
            
    class MockMessage:
        content = [SimpleNamespace(text="Mocked AI response to owner")]
        
    class MockMessages:
        def create(self, *args, **kwargs):
            return MockMessage()
            
    class MockAnthropic:
        def __init__(self, *args, **kwargs):
            self.messages = MockMessages()
            
    monkeypatch.setattr("app.services.messaging.send_whatsapp_text", mock_send)
    import app.database
    monkeypatch.setattr(app.database, "supabase", MockSupabase())
    
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", MockAnthropic)
    
    # Mock review_db.get_pending_finding
    from app.review_sources import db as review_db
    monkeypatch.setattr(review_db, "get_pending_finding", lambda active_store_id: None)
    
    from app.agents.reputation import process_reputation_owner_reply
    process_reputation_owner_reply("+923001234567", "Who served this customer?")
    
    assert len(sent_messages) == 1
    assert "Mocked AI response to owner" in sent_messages[0][1]


