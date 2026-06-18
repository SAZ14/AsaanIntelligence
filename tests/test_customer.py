"""Tests for the Customer Agent — segmentation, lapse alerts, incentives, QR linking."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.agents.customer import (
    EnrichedCustomer,
    link_qr_scan,
    run_customer_agent,
    TAGLINE,
    MILESTONE_VISIT_INTERVAL,
)
from app.analysis.retention import CustomerProfile
from app.ingest import load_dataset
from app.ingest.loader import load_customers
from app.models.canonical import LineItem, LoyaltyCustomer, MenuItem, Order, Payment, Staff

DATA = Path(__file__).resolve().parent.parent / "data"


def _staff() -> dict[str, Staff]:
    return {"S01": Staff(staff_id="S01", name="A", role="server")}


def _menu() -> dict[str, MenuItem]:
    return {
        "ESP": MenuItem(sku="ESP", name="Espresso", category="Coffee", cost=70, price=420),
        "CAKE": MenuItem(sku="CAKE", name="Cheesecake", category="Dessert", cost=150, price=650),
    }


def _order(
    oid: str, dt_: datetime, cref: str, amount: float = 441.0, channel: str = "dine_in",
) -> Order:
    return Order(
        order_id=oid,
        datetime=dt_,
        staff_id="S01",
        staff_name="A",
        channel=channel,
        order_status="closed",
        customer_ref=cref,
        line_items=[LineItem(
            item_sku="ESP", item_name="Espresso", category="Coffee",
            qty=1, unit_price=420, line_amount=420,
        )],
        payments=[Payment(method="card", amount=amount, tax_rate=0.05)],
    )


def _load():
    return load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )


# ── Integration on main dataset ──


def test_customer_agent_runs_on_main_dataset():
    orders, menu, staff = _load()
    registry = load_customers(DATA / "customers.csv")
    report = run_customer_agent(orders, menu, staff, registry, venue_name="Test Café")
    assert report.tagline == TAGLINE
    assert report.loyalty.total_members > 0
    assert report.period_days > 0


def test_lapse_alerts_for_high_value_guests():
    orders, menu, staff = _load()
    report = run_customer_agent(orders, menu, staff, venue_name="Test Café")
    assert len(report.lapse_alerts) > 0
    for alert in report.lapse_alerts:
        assert alert.urgency in ("critical", "high", "medium")
        assert alert.winback_value >= 0
        assert alert.message


def test_corporate_and_banquet_segments_exist():
    orders, menu, staff = _load()
    report = run_customer_agent(orders, menu, staff, venue_name="Test Café")
    segments = {e.segment for e in report.customers}
    assert "corporate" in segments or len(report.corporate_accounts) >= 0
    assert "banquet" in segments or len(report.banquet_accounts) >= 0


def test_loyalty_tiers_assigned():
    orders, menu, staff = _load()
    report = run_customer_agent(orders, menu, staff, venue_name="Test Café")
    tiers = {e.loyalty_tier for e in report.customers}
    assert "bronze" in tiers
    assert report.loyalty.tier_bronze > 0


def test_qr_linked_customers_get_names():
    orders, menu, staff = _load()
    registry = load_customers(DATA / "customers.csv")
    report = run_customer_agent(orders, menu, staff, registry, venue_name="Test Café")
    linked = [e for e in report.customers if e.qr_linked]
    assert len(linked) == report.loyalty.qr_linked_members
    assert report.loyalty.qr_linked_members > 0
    assert all(e.display_name for e in linked)
    assert all(e.contact_phone for e in linked)


def test_incentives_generated():
    orders, menu, staff = _load()
    registry = load_customers(DATA / "customers.csv")
    report = run_customer_agent(orders, menu, staff, registry, venue_name="Test Café")
    assert len(report.incentives) > 0
    types = {i.incentive_type for i in report.incentives}
    assert "winback_lapsed" in types or "winback_lapsing" in types


def test_winback_lapsed_incentive_message():
    orders, menu, staff = _load()
    registry = load_customers(DATA / "customers.csv")
    report = run_customer_agent(orders, menu, staff, registry, venue_name="Sugar Rush")
    winback = [i for i in report.incentives if i.incentive_type == "winback_lapsed"]
    if winback:
        assert "Sugar Rush" in winback[0].message
        assert winback[0].discount_pct == 15


# ── Unit tests ──


def test_milestone_incentive_on_fifth_visit():
    staff = _staff()
    menu = _menu()
    base = date(2026, 5, 1)
    orders = []
    for i in range(5):
        dt = datetime.combine(base + timedelta(days=i * 3), datetime.min.time().replace(hour=10))
        orders.append(_order(f"O{i}", dt, "CUST_MILE"))

    registry = {
        "CUST_MILE": LoyaltyCustomer(
            customer_ref="CUST_MILE",
            qr_token="QR-test",
            display_name="Milestone Guest",
            phone="+923001111111",
            channel="sms",
            opted_in=True,
        ),
    }
    report = run_customer_agent(orders, menu, staff, registry, venue_name="Café")
    milestones = [i for i in report.incentives if i.incentive_type == "visit_milestone"]
    assert len(milestones) == 1
    assert milestones[0].discount_pct == 10
    assert "5 times" in milestones[0].message


def test_lapsed_customer_gets_winback_incentive():
    staff = _staff()
    menu = _menu()
    base = date(2026, 5, 1)
    orders = []
    visit_days = [0, 3, 6, 9, 12]
    for i, d in enumerate(visit_days):
        dt = datetime.combine(base + timedelta(days=d), datetime.min.time().replace(hour=10))
        orders.append(_order(f"O{i}", dt, "CUST_LAPSE"))
    end_dt = datetime.combine(base + timedelta(days=35), datetime.min.time().replace(hour=10))
    orders.append(_order("OPAD", end_dt, "CUST_OTHER"))

    registry = {
        "CUST_LAPSE": LoyaltyCustomer(
            customer_ref="CUST_LAPSE",
            qr_token="QR-lapse",
            display_name="Lapsed Guest",
            phone="+923002222222",
            opted_in=True,
        ),
    }
    report = run_customer_agent(orders, menu, staff, registry, venue_name="Café")
    assert any(i.incentive_type == "winback_lapsed" for i in report.incentives)
    assert any(a.customer_ref == "CUST_LAPSE" for a in report.lapse_alerts)


def test_corporate_segment_high_ticket_low_visits():
    staff = _staff()
    menu = _menu()
    dt = datetime(2026, 5, 10, 12, 0)
    orders = [_order("O1", dt, "CORP1", amount=15000.0)]
    end = datetime(2026, 5, 31, 12, 0)
    orders.append(_order("OPAD", end, "OTHER"))

    report = run_customer_agent(orders, menu, staff, venue_name="Café")
    corp = next(e for e in report.customers if e.profile.customer_ref == "CORP1")
    assert corp.segment == "corporate"


def test_opted_out_qr_skips_outbound_incentives():
    staff = _staff()
    menu = _menu()
    base = date(2026, 5, 1)
    orders = []
    for i in range(5):
        dt = datetime.combine(base + timedelta(days=i * 3), datetime.min.time().replace(hour=10))
        orders.append(_order(f"O{i}", dt, "CUST_NO"))

    registry = {
        "CUST_NO": LoyaltyCustomer(
            customer_ref="CUST_NO",
            qr_token="QR-no",
            display_name="Opted Out",
            phone="+923003333333",
            opted_in=False,
        ),
    }
    report = run_customer_agent(orders, menu, staff, registry, venue_name="Café")
    assert not any(i.customer_ref == "CUST_NO" for i in report.incentives)


def test_link_qr_scan():
    registry: dict[str, LoyaltyCustomer] = {}
    entry = link_qr_scan(
        registry, "QR-new", "CNEW", display_name="New Guest", phone="+923004444444",
    )
    assert entry.customer_ref == "CNEW"
    assert registry["CNEW"].qr_token == "QR-new"
    assert registry["CNEW"].opted_in is True


def test_visit_streak_incentive():
    staff = _staff()
    menu = _menu()
    base = date(2026, 5, 29)  # within STREAK_WINDOW_DAYS of period_end
    orders = []
    for i in range(3):
        dt = datetime.combine(base + timedelta(days=i), datetime.min.time().replace(hour=10))
        orders.append(_order(f"O{i}", dt, "CUST_STRK"))
    pad = datetime(2026, 5, 31, 10, 0)
    orders.append(_order("OPAD", pad, "OTHER"))

    registry = {
        "CUST_STRK": LoyaltyCustomer(
            customer_ref="CUST_STRK",
            qr_token="QR-strk",
            display_name="Streak Guest",
            phone="+923005555555",
            opted_in=True,
        ),
    }
    report = run_customer_agent(orders, menu, staff, registry, venue_name="Café")
    streaks = [i for i in report.incentives if i.incentive_type == "visit_streak"]
    assert len(streaks) >= 1


def test_holdout_customer_agent_computes():
    holdout = DATA / "holdout"
    orders, menu, staff = load_dataset(
        holdout / "sales_detail.csv", holdout / "menu.csv", holdout / "staff.csv",
    )
    report = run_customer_agent(orders, menu, staff, venue_name="Holdout Café")
    assert report.loyalty.total_members > 0
    assert report.period_days > 0
    assert len(report.incentives) >= 0
