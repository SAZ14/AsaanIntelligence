from pathlib import Path

from app.ingest import load_dataset
from app.analysis.integrity import analyze_integrity
from app.analysis.retention import analyze_retention, analyze_operations
from app.report.render import generate_report, compute_headlines

DATA = Path(__file__).resolve().parent.parent / "data"


def _build():
    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )
    integrity = analyze_integrity(orders, menu, staff)
    retention = analyze_retention(orders, menu, staff)
    operations = analyze_operations(orders, menu, staff)
    return integrity, retention, operations


def test_report_generates_without_error():
    integrity, retention, operations = _build()
    html = generate_report(integrity, retention, operations)
    assert len(html) > 1000
    assert "<html" in html
    assert "</html>" in html


def test_headline_numbers_present_and_positive():
    integrity, retention, operations = _build()
    h = compute_headlines(integrity, retention, operations)
    assert h.monthly_leakage > 0, f"Leakage should be positive, got {h.monthly_leakage}"
    assert h.monthly_winback_tier_a > 0, f"Win-back Tier A should be positive, got {h.monthly_winback_tier_a}"
    assert h.monthly_winback_total >= h.monthly_winback_tier_a


def test_headline_numbers_in_html():
    integrity, retention, operations = _build()
    h = compute_headlines(integrity, retention, operations)
    html = generate_report(integrity, retention, operations)
    assert f"PKR {h.monthly_leakage:,.0f}" in html
    assert f"PKR {h.monthly_winback_tier_a:,.0f}" in html


def test_flagged_staff_drive_leakage():
    integrity, retention, operations = _build()
    h = compute_headlines(integrity, retention, operations)
    assert len(h.monthly_leakage_flagged_staff) >= 1
    assert h.monthly_leakage <= h.venue_wide_leakage_monthly


def test_winnable_lapsed_within_gap_limit():
    integrity, retention, operations = _build()
    h = compute_headlines(integrity, retention, operations)
    for c in h.tier_a_winnable + h.tier_b_winnable:
        assert c.days_since_last <= 30
