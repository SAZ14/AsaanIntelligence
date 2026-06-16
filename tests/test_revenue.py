"""Property-based tests for the Revenue agent, run against the shipped café
dataset. NLU uses the deterministic fallback (no network / no Claude key).
"""

from __future__ import annotations

from datetime import date

import pytest

from app.revenue.agent import RevenueAgent, render_digest_text
from app.revenue.analytics import (
    detect_dead_windows,
    product_performance,
    recommend_campaigns,
)
from app.revenue.config import RevenueConfig
from app.revenue.datasource import filter_period, load_pos, normalize_period
from app.revenue.nlu import parse_query
from app.revenue.pricing import compute_pricing_recommendations, _headroom, _stability
from app.revenue.segments import build_segments
from app.revenue.store import Store
from app.revenue.strategy import (
    analyze_attach,
    build_playbook,
    frequency_opportunities,
    menu_opportunities,
)

AS_OF = date(2026, 5, 31)


@pytest.fixture(scope="module")
def pos():
    return load_pos()


@pytest.fixture(scope="module")
def month(pos):
    orders, _menu, _staff = pos
    window, _s, _e = filter_period(orders, "month", AS_OF)
    return window


def _agent(pos) -> RevenueAgent:
    orders, menu, staff = pos
    return RevenueAgent(orders=orders, menu=menu, staff=staff,
                        config=RevenueConfig(), client=None, as_of=AS_OF)


# ── datasource / period ──

class TestPeriod:
    def test_normalize_aliases(self):
        assert normalize_period("today") == "day"
        assert normalize_period("this week") == "week"
        assert normalize_period("monthly") == "month"
        assert normalize_period("garbage") == "week"

    def test_trailing_window_bounds(self, pos):
        orders, _m, _s = pos
        window, start, end = filter_period(orders, "week", AS_OF)
        assert end == AS_OF
        assert (end - start).days == 6
        assert all(start <= o.datetime.date() <= end for o in window)

    def test_day_window_smaller_than_month(self, pos):
        orders, _m, _s = pos
        day, _, _ = filter_period(orders, "day", AS_OF)
        month, _, _ = filter_period(orders, "month", AS_OF)
        assert 0 < len(day) <= len(month) < len(orders)


# ── product performance ──

class TestProductPerformance:
    def test_top_sellers_have_units(self, pos, month):
        _o, menu, staff = pos
        perf = product_performance(month, menu, staff, period_label="month")
        assert perf.top_sellers
        assert all(s.units > 0 for s in perf.top_sellers)
        # sorted by volume descending
        units = [s.units for s in perf.top_sellers]
        assert units == sorted(units, reverse=True)

    def test_revenue_and_orders_positive(self, pos, month):
        _o, menu, staff = pos
        perf = product_performance(month, menu, staff)
        assert perf.total_revenue > 0
        assert perf.order_count == len(month)
        assert perf.avg_ticket > 0


# ── pricing power ──

class TestPricing:
    def test_recommendations_are_well_formed(self, pos, month):
        _o, menu, _s = pos
        recs = compute_pricing_recommendations(month, menu, RevenueConfig(), period_days=30)
        assert recs, "expected at least one pricing-power candidate"
        for r in recs:
            assert r.suggested_price > r.current_price
            assert 0.03 <= r.raise_pct <= 0.10
            assert r.power_score >= RevenueConfig().min_power_score
            assert r.est_monthly_uplift > 0
            assert r.units >= RevenueConfig().min_volume_for_pricing

    def test_habitual_coffee_surfaces(self, pos, month):
        _o, menu, _s = pos
        recs = compute_pricing_recommendations(month, menu, RevenueConfig(), period_days=30)
        cats = {r.category for r in recs}
        assert "Coffee" in cats  # habitual, steady demand → pricing power

    def test_uplift_sorted_descending(self, pos, month):
        _o, menu, _s = pos
        recs = compute_pricing_recommendations(month, menu, RevenueConfig(), period_days=30)
        ups = [r.est_monthly_uplift for r in recs]
        assert ups == sorted(ups, reverse=True)

    def test_stability_and_headroom_bounds(self):
        assert _stability([10, 10, 10, 10]) == 1.0       # perfectly steady
        assert _stability([0, 20, 0, 20]) < 0.5          # spiky
        assert _headroom(100, [100, 200]) == 1.0         # cheapest → max room
        assert _headroom(200, [100, 200]) == 0.0         # priciest → no room


# ── dead windows ──

class TestDeadWindows:
    def test_windows_sorted_ascending_with_gap(self, month):
        windows = detect_dead_windows(month, top_n=3)
        assert windows
        avgs = [w.avg_orders for w in windows]
        assert avgs == sorted(avgs)                      # quietest first
        assert windows[0].gap_vs_peak > 0                # below the peak window

    def test_quiet_daypart_detected(self, month):
        # The dataset plants weekday-afternoon lulls; deadest windows should be
        # in genuinely quiet dayparts, not the morning/evening rushes.
        windows = detect_dead_windows(month, top_n=3)
        quiet = {"Afternoon", "Midday", "Late night", "Early morning"}
        assert any(w.daypart in quiet for w in windows)


# ── segments & campaigns ──

class TestSegmentsAndCampaigns:
    def test_segments_built(self, pos, month):
        _o, menu, staff = pos
        segs = build_segments(month, menu, staff, RevenueConfig())
        assert segs["premium"].size > 0
        assert 0 < segs["premium"].redemption_rate < 1
        assert segs["premium"].expected_redemptions() >= 0

    def test_campaigns_are_brand_safe_and_quantified(self, pos, month):
        _o, menu, staff = pos
        segs = build_segments(month, menu, staff, RevenueConfig())
        windows = detect_dead_windows(month, top_n=3)
        perf = product_performance(month, menu, staff)
        recs = recommend_campaigns(windows, segs, RevenueConfig(), perf.avg_ticket)
        assert recs
        for r in recs:
            assert r.audience_size > 0
            assert r.expected_redemptions >= 0
            assert "underutilized" in r.message
            # Owner-facing framing: the owner runs it, we don't message customers.
            assert "Run" in r.message and "Send" not in r.message
            # Brand-safe: no cheap "20% off" / discount coupons.
            assert "%" not in r.campaign_name
            assert "discount" not in r.campaign_name.lower()


# ── strategy levers ──

class TestStrategy:
    def test_attach_insight_well_formed(self, pos, month):
        _o, menu, _s = pos
        a = analyze_attach(month, menu, RevenueConfig(), period_days=30)
        assert a.beverage_orders > 0
        assert 0.0 <= a.attach_rate <= 1.0
        assert a.target_rate >= a.attach_rate
        assert a.avg_food_price > 0
        assert a.est_monthly_uplift >= 0

    def test_menu_moves_flag_dog_and_hero(self, pos, month):
        _o, menu, staff = pos
        moves = menu_opportunities(month, menu, staff, RevenueConfig())
        kinds = {m.kind for m in moves}
        assert "feature" in kinds                      # push a high-margin hero
        # Imported Soda is the planted low-margin "dog".
        assert any(m.kind == "fix" and "Soda" in m.name for m in moves)
        assert "add" in kinds                          # suggest a missing category

    def test_frequency_insight(self, pos, month):
        _o, menu, staff = pos
        f = frequency_opportunities(month, menu, staff, RevenueConfig())
        assert 0.0 <= f.repeat_rate <= 1.0
        assert f.regulars >= 0
        assert f.loyalty_note

    def test_playbook_covers_levers(self, pos, month):
        _o, menu, staff = pos
        pb = build_playbook(month, menu, staff, RevenueConfig(), period_days=30)
        assert pb.headline
        assert len(pb.items) >= 3
        levers = {it.lever for it in pb.items}
        assert "Average ticket" in levers
        # items with a modelled impact are ordered ahead of unknowns
        impacts = [it.est_monthly_impact for it in pb.items if it.est_monthly_impact]
        assert impacts == sorted(impacts, reverse=True)


# ── NLU routing ──

class TestNLU:
    def test_intents(self):
        assert parse_query("how did we do this week").intent == "summary"
        assert parse_query("what are the best sellers").intent == "best_sellers"
        assert parse_query("what can I raise prices on").intent == "pricing"
        assert parse_query("when are we slow?").intent == "dead_windows"
        assert parse_query("give me campaign ideas").intent == "campaigns"
        assert parse_query("send me a weekly digest").intent == "subscribe"
        assert parse_query("help").intent == "help"

    def test_strategy_lever_intents(self):
        assert parse_query("how do I grow revenue?").intent == "strategy"
        assert parse_query("how do I maximise revenue").intent == "strategy"
        assert parse_query("how do I raise the average ticket").intent == "upsell"
        assert parse_query("any combo or bundle ideas").intent == "upsell"
        assert parse_query("give me menu advice").intent == "menu"
        assert parse_query("how do I get repeat customers").intent == "loyalty"

    def test_period_detection(self):
        assert parse_query("revenue today").period == "day"
        assert parse_query("best sellers this month").period == "month"
        assert parse_query("how are sales").period == "week"


# ── agent routing & subscriptions ──

class TestAgent:
    def test_routes_each_intent(self, pos):
        agent = _agent(pos)
        assert agent.handle_message("+92300", "how did we do this week").intent == "summary"
        assert agent.handle_message("+92300", "best sellers this month").intent == "best_sellers"
        assert agent.handle_message("+92300", "what can I raise prices on").intent == "pricing"
        assert agent.handle_message("+92300", "when are we slow").intent == "dead_windows"

    def test_routes_strategy_levers(self, pos):
        agent = _agent(pos)
        assert agent.handle_message("+92300", "how do I grow revenue").intent == "strategy"
        assert agent.handle_message("+92300", "raise the average ticket").intent == "upsell"
        assert agent.handle_message("+92300", "menu advice").intent == "menu"
        assert agent.handle_message("+92300", "how do I get repeat business").intent == "loyalty"

    def test_strategy_reply_has_actions(self, pos):
        agent = _agent(pos)
        reply = agent.handle_message("+92300", "how do I grow revenue")
        assert reply.action == "advise"
        assert len(reply.text) > 80
        assert "Average ticket" in reply.text

    def test_data_answers_always_advise_growth(self, pos):
        # A revenue agent should never just report — every data answer must
        # close with the top 2-3 growth recommendations.
        agent = _agent(pos)
        for q in ["how did we do this week", "best sellers this month",
                  "what can I raise prices on", "when are we slow"]:
            reply = agent.handle_message("+92300", q)
            assert "Top moves to grow revenue" in reply.text, q
            # at least two ranked moves are surfaced
            assert "1." in reply.text and "2." in reply.text, q

    def test_campaigns_are_logged(self, pos):
        agent = _agent(pos)
        reply = agent.handle_message("+92300", "campaign ideas")
        assert reply.campaigns_logged > 0
        assert len(agent.store.list_campaigns()) == reply.campaigns_logged

    def test_subscribe_persists_and_digest_generates(self, pos):
        agent = _agent(pos)
        r = agent.handle_message("+923009998888", "send me a weekly digest")
        assert r.action == "subscribed"
        sub = agent.store.get_subscription("+923009998888")
        assert sub is not None and sub.cadence == "weekly"
        digests = agent.generate_due_digests("weekly")
        assert digests
        assert any(phone == "+923009998888" for d in digests for phone, _ in d.outbound)

    def test_digest_text_has_venue_and_revenue(self, pos):
        agent = _agent(pos)
        digest = agent.build_owner_digest("week")
        text = render_digest_text(digest, agent.config.venue_name)
        assert "Sugar Rush" in text
        assert "Revenue" in text

    def test_unknown_falls_back_to_help(self, pos):
        agent = _agent(pos)
        reply = agent.handle_message("+92300", "asdfghjkl")
        assert reply.intent == "help"
