"""Tests for daily / weekly period reports and their digests."""

from datetime import timedelta
from pathlib import Path

from app.analysis.periodic import (
    build_daily_report,
    build_weekly_report,
    latest_date,
    slice_orders,
)
from app.pos import RestaurantConfig, build_connector
from app.whatsapp.service import IntegrityWhatsAppService

DATA = Path(__file__).resolve().parent.parent / "data"

ROASTERY = RestaurantConfig(
    venue_name="Roastery",
    pos_type="csv",
    connection={"base_dir": str(DATA)},
    mapping="cafe_generic",
)


def _svc() -> IntegrityWhatsAppService:
    return IntegrityWhatsAppService(
        restaurants={"roastery": ROASTERY},
        owner_map={"whatsapp:+100": "roastery"},
        default_venue="roastery",
    )


def _data():
    return build_connector(ROASTERY).fetch()


def test_slice_orders_is_inclusive_and_windowed():
    data = _data()
    end = latest_date(data.orders)
    start = end - timedelta(days=6)

    windowed = slice_orders(data.orders, start, end)
    assert windowed, "expected orders in the last 7 days"
    assert all(start <= o.datetime.date() <= end for o in windowed)
    # The window is a strict subset of the full 35-day dataset.
    assert len(windowed) < len(data.orders)


def test_daily_report_covers_a_single_day():
    data = _data()
    p = build_daily_report(data.orders, data.menu, data.staff, venue_name="Roastery")
    assert p is not None
    assert p.kind == "daily"
    assert p.start == p.end == latest_date(data.orders)
    # Period is exactly one day.
    assert p.report.reconciliation.period_days == 1
    # Comparison against the prior day is attached.
    assert p.previous is not None


def test_weekly_report_covers_seven_days_with_daily_breakdown():
    data = _data()
    p = build_weekly_report(data.orders, data.menu, data.staff, venue_name="Roastery")
    assert p is not None
    assert p.kind == "weekly"
    assert (p.end - p.start).days == 6
    assert len(p.days) == 7
    # The week's net sales equal the sum of its per-day net sales.
    daily_sum = sum(d.net_sales for d in p.days)
    assert abs(daily_sum - p.report.reconciliation.net_sales) < 1.0


def test_daily_window_is_smaller_than_weekly():
    data = _data()
    daily = build_daily_report(data.orders, data.menu, data.staff)
    weekly = build_weekly_report(data.orders, data.menu, data.staff)
    assert daily.report.reconciliation.net_sales <= weekly.report.reconciliation.net_sales


def test_empty_orders_yield_no_report():
    assert build_daily_report([], {}, {}) is None
    assert build_weekly_report([], {}, {}) is None


def test_service_digests_render_text():
    svc = _svc()
    daily = svc.daily_digest("roastery")
    weekly = svc.weekly_digest("roastery")
    assert "Daily report" in daily
    assert "Net sales" in daily
    assert "Weekly summary" in weekly
    assert "Leakage this week" in weekly


def test_daily_and_weekly_commands_route():
    svc = _svc()
    assert "Daily report" in svc.handle_message("whatsapp:+100", "daily")
    assert "Weekly summary" in svc.handle_message("whatsapp:+100", "weekly")


def test_period_pdfs_render_valid_bytes():
    from app.report.pdf import build_period_pdf

    data = _data()
    daily = build_daily_report(data.orders, data.menu, data.staff, venue_name="Roastery")
    weekly = build_weekly_report(data.orders, data.menu, data.staff, venue_name="Roastery")
    for p in (daily, weekly):
        pdf = build_period_pdf(p)
        assert pdf.startswith(b"%PDF-1.4")
        assert pdf.rstrip().endswith(b"%%EOF")
        assert len(pdf) > 2000


def test_delta_direction_tags():
    # Revenue up is good (green), leakage up is bad (red).
    assert "🟢" in IntegrityWhatsAppService._delta(110, 100, good_up=True)
    assert "🔴" in IntegrityWhatsAppService._delta(110, 100, good_up=False)
    assert IntegrityWhatsAppService._delta(100, 100) == "→ flat"
    assert IntegrityWhatsAppService._delta(5, 0) == "(new)"
