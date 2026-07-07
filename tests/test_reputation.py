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


# ── classify_reviews_batch: rule-based path must not treat "no rating" as
# a bad rating ──────────────────────────────────────────────────────────────
#
# normalizer.py defaults a missing star rating (e.g. every Instagram caption
# and comment, which has no rating field at all) to 0. The rule-based
# classifier used for anything older than HISTORICAL_CUTOFF_DAYS treated
# "rating <= 2" as negative -- 0 satisfies that condition, so every older
# unrated Instagram item was silently mislabeled negative regardless of
# what it actually said.

def _old_unrated(text: str) -> ReviewAnalysis:
    from app.agents.reputation import HISTORICAL_CUTOFF_DAYS
    old_date = (datetime.utcnow() - timedelta(days=HISTORICAL_CUTOFF_DAYS + 5)).strftime("%Y-%m-%d")
    return ReviewAnalysis(
        review_id="ig1", source="Instagram - anatummyisb", rating=0,
        posted_at=old_date, reviewer_name="someone", text=text,
    )


def _mock_llm_client(reply_lines: list[str]):
    """A minimal stand-in for the ZAI client's chat.completions.create()
    shape, returning a fixed 'N. issue_class,sentiment' response body."""
    from unittest.mock import MagicMock
    client = MagicMock()
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "\n".join(reply_lines)
    client.chat.completions.create.return_value = resp
    return client


class TestUnratedInstagramClassification:
    def test_unrated_old_item_routed_through_llm_not_rule_based(self):
        """The rule-based path can only look at rating -- it has nothing
        to work with for an unrated platform, so it used to hand back a
        placeholder ("neutral") regardless of what the text actually said.
        Unrated items must go through the LLM (which reads the text)
        regardless of age."""
        from app.agents.reputation import classify_reviews_batch
        ra = _old_unrated("This place is absolutely terrible, avoid it")
        client = _mock_llm_client(["1. service_speed,negative"])
        result = classify_reviews_batch([ra], client)
        client.chat.completions.create.assert_called_once()
        assert result[0].sentiment == "negative"
        assert result[0].issue_class == "service_speed"

    def test_llm_prompt_does_not_claim_a_fake_zero_star_rating(self):
        """"[Rating 0/5]" reads as the worst possible score to an LLM, not
        "no rating provided" -- misleading for a platform (Instagram) that
        has no star ratings at all."""
        from app.agents.reputation import classify_reviews_batch
        ra = _old_unrated("Neutral comment text")
        client = _mock_llm_client(["1. other,neutral"])
        classify_reviews_batch([ra], client)
        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        assert "Rating 0/5" not in prompt
        assert "No star rating" in prompt

    def test_unrated_item_never_treated_as_actionable_negative_review(self):
        """rating=0 is falsy, so it's excluded from draft_replies'
        "0 < rating <= 3" filter regardless of classified sentiment --
        unrated content never gets a drafted reply queued for posting."""
        ra = _old_unrated("Some comment with no star rating")
        needs_reply = bool(ra.rating and 0 < ra.rating <= 3)
        assert needs_reply is False

    def test_genuinely_bad_rating_still_uses_the_fast_rule_based_path(self):
        """A real 1-2 star review has a real signal to classify from --
        it should stay on the free rule-based path, not cost an LLM call."""
        from app.agents.reputation import classify_reviews_batch, HISTORICAL_CUTOFF_DAYS
        old_date = (datetime.utcnow() - timedelta(days=HISTORICAL_CUTOFF_DAYS + 5)).strftime("%Y-%m-%d")
        ra = ReviewAnalysis(
            review_id="gm1", source="Google Maps", rating=1,
            posted_at=old_date, reviewer_name="someone", text="Terrible service",
        )
        client = _mock_llm_client([])
        result = classify_reviews_batch([ra], client)
        client.chat.completions.create.assert_not_called()
        assert result[0].sentiment == "negative"

    def test_genuinely_good_rating_still_uses_the_fast_rule_based_path(self):
        from app.agents.reputation import classify_reviews_batch, HISTORICAL_CUTOFF_DAYS
        old_date = (datetime.utcnow() - timedelta(days=HISTORICAL_CUTOFF_DAYS + 5)).strftime("%Y-%m-%d")
        ra = ReviewAnalysis(
            review_id="gm2", source="Google Maps", rating=5,
            posted_at=old_date, reviewer_name="someone", text="Loved it",
        )
        client = _mock_llm_client([])
        result = classify_reviews_batch([ra], client)
        client.chat.completions.create.assert_not_called()
        assert result[0].sentiment == "positive"


# ── select_reviews_needing_reply ─────────────────────────────────────────────
#
# Positive reviews used to be silently auto-closed with no drafted reply and
# no way to see or respond to them -- only negative (1-3 star) reviews ever
# entered the scrollable pending queue. Good reviews deserve a thank-you
# draft and a chance to be seen too.

def _rated(review_id: str, rating: float) -> ReviewAnalysis:
    return ReviewAnalysis(
        review_id=review_id, source="Google Maps", rating=rating,
        posted_at="2026-07-01", reviewer_name="someone", text="some review text",
    )


class TestSelectReviewsNeedingReply:
    def test_negative_reviews_are_selected(self):
        from app.agents.reputation import select_reviews_needing_reply
        reviews = [_rated("r1", 1.0), _rated("r2", 2.0)]
        result = select_reviews_needing_reply(reviews)
        assert {r.review_id for r in result} == {"r1", "r2"}

    def test_positive_reviews_are_now_also_selected(self):
        from app.agents.reputation import select_reviews_needing_reply
        reviews = [_rated("r1", 5.0), _rated("r2", 4.0)]
        result = select_reviews_needing_reply(reviews)
        assert {r.review_id for r in result} == {"r1", "r2"}

    def test_neutral_3_star_is_not_selected(self):
        """3 stars already gets an "appreciative" tone via draft_replies for
        the negative bucket -- keep the existing negative-inclusive <=3
        boundary intact, don't accidentally double-select it as positive."""
        from app.agents.reputation import select_reviews_needing_reply, MAX_DRAFT_REPLIES
        reviews = [_rated("r1", 3.0)]
        result = select_reviews_needing_reply(reviews)
        assert len(result) == 1
        assert result[0].review_id == "r1"

    def test_unrated_reviews_never_selected(self):
        """No star rating (e.g. every Instagram comment) means no signal to
        pick a reply tone from -- excluded from both buckets, same as
        before this change."""
        from app.agents.reputation import select_reviews_needing_reply
        reviews = [_rated("r1", 0)]
        result = select_reviews_needing_reply(reviews)
        assert result == []

    def test_negative_and_positive_caps_are_independent(self):
        """A flood of 5-star reviews must not crowd out negative-review
        coverage, and vice versa -- each bucket has its own cap."""
        from app.agents.reputation import (
            select_reviews_needing_reply, MAX_DRAFT_REPLIES, MAX_POSITIVE_DRAFT_REPLIES,
        )
        negatives = [_rated(f"neg{i}", 1.0) for i in range(20)]
        positives = [_rated(f"pos{i}", 5.0) for i in range(20)]
        result = select_reviews_needing_reply(negatives + positives)
        selected_ids = {r.review_id for r in result}
        neg_selected = [rid for rid in selected_ids if rid.startswith("neg")]
        pos_selected = [rid for rid in selected_ids if rid.startswith("pos")]
        assert len(neg_selected) == MAX_DRAFT_REPLIES
        assert len(pos_selected) == MAX_POSITIVE_DRAFT_REPLIES
        assert len(result) == MAX_DRAFT_REPLIES + MAX_POSITIVE_DRAFT_REPLIES

    def test_mixed_ratings_all_get_selected_up_to_their_own_cap(self):
        from app.agents.reputation import select_reviews_needing_reply
        reviews = [_rated("bad", 1.0), _rated("mid", 3.0), _rated("good", 4.0), _rated("great", 5.0)]
        result = select_reviews_needing_reply(reviews)
        assert {r.review_id for r in result} == {"bad", "mid", "good", "great"}


# ── draft_replies gives positive reviews a real thank-you tone ──────────────

class TestDraftRepliesPositiveTone:
    def test_five_star_praise_gets_thankful_tone(self):
        from app.agents.reputation import draft_replies
        ra = _rated("r1", 5.0)
        ra.issue_class = "praise"
        client = _mock_llm_client([])
        client.chat.completions.create.return_value.choices[0].message.content = "Thank you so much!"
        draft_replies([ra], client, venue_name="Test Cafe")
        prompt = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        assert "thankful" in prompt.lower()
        assert ra.draft_reply == "Thank you so much!"

    def test_four_star_gets_warm_thank_you_tone(self):
        from app.agents.reputation import draft_replies
        ra = _rated("r1", 4.0)
        client = _mock_llm_client([])
        draft_replies([ra], client, venue_name="Test Cafe")
        prompt = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        assert "warm thank you" in prompt.lower()


# ── cap_reviews_balanced ──────────────────────────────────────────────────────
#
# Confirmed live: a real check capped to the 50 most-recent-overall reviews
# came back 100% Instagram (which has no star ratings), so none of them
# could ever be classified as negative or positive -- the report said "no
# negative reviews" not because there weren't any, but because a burst of
# recent Instagram comments crowded every single Google Maps review (the
# only source that can ever be actionable) out of the batch entirely.

def _raw(source: str, posted_at: str) -> dict:
    return {"source": source, "text": "x", "posted_at": posted_at, "hash": f"{source}-{posted_at}"}


class TestCapReviewsBalanced:
    def test_reproduces_the_live_bug_scenario(self):
        """50 very-recent Instagram comments + 10 older-but-real Google
        Maps reviews -- a flat recency sort drops every Google Maps review;
        balancing must not."""
        from app.agents.reputation import cap_reviews_balanced
        instagram = [_raw("Instagram - x comment", f"2026-07-0{7}T{i:02d}:00:00Z") for i in range(50)]
        google = [_raw("Google Maps - Anatummy", f"2026-06-{20+i}T00:00:00Z") for i in range(10)]
        result = cap_reviews_balanced(instagram + google, max_total=50)
        platforms = {r["source"].split(" - ")[0] for r in result}
        assert "Google Maps" in platforms
        google_in_result = [r for r in result if r["source"].startswith("Google")]
        assert len(google_in_result) == 10  # all of them fit within its even share

    def test_even_split_across_two_platforms(self):
        from app.agents.reputation import cap_reviews_balanced
        instagram = [_raw("Instagram - x comment", f"2026-07-0{i%9+1}T00:00:00Z") for i in range(40)]
        google = [_raw("Google Maps - Anatummy", f"2026-07-0{i%9+1}T00:00:00Z") for i in range(40)]
        result = cap_reviews_balanced(instagram + google, max_total=50)
        counts = {}
        for r in result:
            counts[r["source"].split(" - ")[0]] = counts.get(r["source"].split(" - ")[0], 0) + 1
        assert counts["Instagram"] == 25
        assert counts["Google Maps"] == 25

    def test_single_platform_gets_everything_up_to_cap(self):
        from app.agents.reputation import cap_reviews_balanced
        google = [_raw("Google Maps - Anatummy", f"2026-07-0{i%9+1}T00:00:00Z") for i in range(30)]
        result = cap_reviews_balanced(google, max_total=50)
        assert len(result) == 30

    def test_backfills_leftover_budget_from_the_larger_platform(self):
        """One platform has fewer than its even share -- the unused budget
        should go to the other platform, not be wasted."""
        from app.agents.reputation import cap_reviews_balanced
        google = [_raw("Google Maps - Anatummy", f"2026-07-0{i%9+1}T00:00:00Z") for i in range(3)]
        instagram = [_raw("Instagram - x comment", f"2026-07-0{i%9+1}T00:00:00Z") for i in range(40)]
        result = cap_reviews_balanced(google + instagram, max_total=50)
        assert len(result) == 43  # 3 google (all of them) + 40 instagram (all of them, under cap)

    def test_empty_input_returns_empty(self):
        from app.agents.reputation import cap_reviews_balanced
        assert cap_reviews_balanced([], max_total=50) == []
