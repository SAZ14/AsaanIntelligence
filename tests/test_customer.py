"""Tests for the live Customer agent.

Properties, not answer keys — the lapsed count is data-derived and may vary.
Covers: the agent consuming a RetentionReport, the report surfacing the
recovery-adjusted total (NOT the naive winback_value), alert formatting, and
DRY_RUN delivery (which keeps CI green without Twilio/credentials).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.agents.customer import (
    CustomerDescriptor,
    format_lapsed_vip_alert,
    recovery_headlines,
    run_customer_agent,
)
from app.models.canonical import LineItem, MenuItem, Order, Payment, Staff
from app.report.render import _observed_monthly_spend, compute_headlines
from app.analysis.retention import analyze_retention, OperationsReport
from app.analysis.integrity import IntegrityReport, VenueBaseline
from app.whatsapp import WhatsAppNotifier


# ── Fixtures: a small venue with a high-value lapsed regular ──

MENU = {
    "ESP": MenuItem(sku="ESP", name="Espresso", category="Coffee", cost=70, price=420),
    "CAKE": MenuItem(sku="CAKE", name="Cheesecake", category="Dessert", cost=200, price=800),
}
STAFF = {"S01": Staff(staff_id="S01", name="A", role="server")}
BASE = date(2026, 5, 1)
PERIOD_END_DAY = 35


def _order(ref: str, day: int, hour: int, amount: float, sku: str = "ESP") -> Order:
    dt = datetime.combine(BASE + timedelta(days=day), datetime.min.time().replace(hour=hour))
    mi = MENU[sku]
    return Order(
        order_id=f"O{ref}{day:02d}", datetime=dt,
        staff_id="S01", staff_name="A", channel="dine_in",
        order_status="closed", customer_ref=ref,
        line_items=[LineItem(item_sku=sku, item_name=mi.name, category=mi.category,
                             qty=1, unit_price=mi.price, line_amount=amount)],
        payments=[Payment(method="card", amount=amount * 1.05, tax_rate=0.05)],
    )


def _synthetic_orders() -> list[Order]:
    orders: list[Order] = []
    # VIP_LAPSED — frequent & high-ticket early, then stops (winnable gap).
    for d in [0, 2, 4, 6, 8, 10]:
        orders.append(_order("VIP_LAPSED", d, 9, 2000, sku="CAKE"))
    # STEADY — visits across the whole period at ~3-day cadence (not lapsing).
    for d in range(0, PERIOD_END_DAY + 1, 3):
        orders.append(_order("STEADY", d, 10, 500))
    # OCCASIONAL — two early visits, low spend.
    for d in [1, 5]:
        orders.append(_order("OCCASIONAL", d, 11, 420))
    # NEWBIE — single recent first visit near period end.
    orders.append(_order("NEWBIE", PERIOD_END_DAY - 2, 12, 420))
    # Anonymous cash orders (blank ref) so coverage < 100%.
    for d in [3, 7, 11]:
        o = _order("", d, 13, 600)
        o.payments[0].method = "cash"
        orders.append(o)
    return orders


def _retention():
    orders = _synthetic_orders()
    return analyze_retention(orders, MENU, STAFF), orders


# ── 1. Agent consumes the RetentionReport ──

def test_agent_consumes_retention_report():
    ret, orders = _retention()
    report = run_customer_agent(ret, orders, MENU, STAFF)

    # Frequency tiers partition every recognised customer exactly once.
    seg = report.segments
    partitioned = len(seg.regulars) + len(seg.new) + len(seg.occasional)
    assert partitioned == report.unique_customers
    assert report.unique_customers == ret.unique_customers

    # Coverage is reported and below 100% (anonymous cash excluded).
    assert 0.0 < report.coverage_order_pct < 1.0

    # Per-customer recovery value mirrors render's recovery-adjusted formula.
    by_ref = {p.customer_ref: p for p in ret.customers}
    for d in report.lapsed:
        prof = by_ref[d.customer_ref]
        expected = _observed_monthly_spend(prof) * report.recovery_rate
        assert d.recovery_adjusted_value == pytest.approx(expected)


def test_lapsed_list_matches_tier_a_winnable():
    ret, orders = _retention()
    headlines = recovery_headlines(ret, orders)
    report = run_customer_agent(ret, orders, MENU, STAFF, headlines=headlines)

    assert {d.customer_ref for d in report.lapsed} == {
        p.customer_ref for p in headlines.tier_a_winnable
    }
    # The planted high-value lapser should be flagged, and as a VIP.
    refs = {d.customer_ref for d in report.lapsed}
    assert "VIP_LAPSED" in refs
    assert "VIP_LAPSED" in {d.customer_ref for d in report.lapsed_vips}


def test_descriptor_has_behaviour_no_identity():
    ret, orders = _retention()
    report = run_customer_agent(ret, orders, MENU, STAFF)
    vip = next(d for d in report.lapsed if d.customer_ref == "VIP_LAPSED")
    assert vip.visit_count == 6
    assert vip.median_cadence_days == pytest.approx(2.0)
    assert vip.days_since_last > 0
    assert "Cheesecake" in vip.top_items  # behavioural descriptor: usual order


# ── 2. Recovery-adjusted total, never the naive winback_value ──

def test_report_uses_recovery_adjusted_total_not_naive():
    ret, orders = _retention()
    # Build real headlines the canonical way to compare against.
    integ = IntegrityReport(venue_baseline=VenueBaseline(period_days=PERIOD_END_DAY + 1))
    headlines = compute_headlines(integ, ret, OperationsReport())
    report = run_customer_agent(ret, orders, MENU, STAFF, headlines=headlines)

    assert report.total_recoverable_monthly == pytest.approx(headlines.monthly_winback_tier_a)

    # The naive winback_value total is a DIFFERENT (larger) number — prove we
    # are not reporting it.
    naive_total = sum(p.winback_value for p in ret.customers if p.is_lapsed_regular)
    assert naive_total > 0
    assert report.total_recoverable_monthly != pytest.approx(naive_total)
    assert report.total_recoverable_monthly < naive_total


# ── 3. Alert formatting ──

def test_lapsed_vip_alert_format():
    d = CustomerDescriptor(
        customer_ref="Cabc123", visit_count=6, median_cadence_days=2.0,
        days_since_last=25, last_visit=date(2026, 5, 11), avg_ticket=2000.0,
        total_spend=12000.0, top_items=["Cheesecake", "Espresso"],
        is_vip=True, recovery_adjusted_value=8500.0,
    )
    alert = format_lapsed_vip_alert(d, venue_name="Sugar Rush")

    assert "Cabc123" in alert                      # recognised by token
    assert "Cheesecake" in alert                   # behavioural descriptor
    assert "25 days ago" in alert                  # last seen
    assert "every 2 days" in alert                 # cadence
    assert "8,500" in alert                        # recovery-adjusted value
    assert "Recovery-adjusted" in alert
    assert "token only" in alert                   # no identity claim


# ── 4. DRY_RUN delivery stays green ──

def test_notifier_dry_run_records_without_sending():
    n = WhatsAppNotifier(dry_run=True)
    msg = n.send("+920000000000", "hello")
    assert msg.status == "dry_run"
    assert msg.to == "whatsapp:+920000000000"
    assert len(n.sent) == 1


def test_send_lapsed_vip_alerts_dry_run():
    from scripts.customer_live import send_lapsed_vip_alerts

    ret, orders = _retention()
    report = run_customer_agent(ret, orders, MENU, STAFF)
    n = WhatsAppNotifier(dry_run=True)
    sent = send_lapsed_vip_alerts(report, n, "whatsapp:+920000000000")

    assert len(sent) == len(report.lapsed_vips)
    assert len(sent) >= 1
    assert all(m.status == "dry_run" for m in sent)
    assert all("PKR" in m.body for m in sent)
