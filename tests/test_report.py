from pathlib import Path

from app.core.ingest import load_dataset
from app.agents.integrity.analyzer import analyze_integrity
from app.agents.retention.analyzer import analyze_retention
from app.agents.operations.analyzer import analyze_operations
from app.reporting.render import (
    generate_report, compute_headlines,
    _observed_monthly_spend, SANITY_WINBACK_PCT_WARN,
)

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
    assert h.monthly_leakage > 0
    assert h.monthly_winback_tier_a > 0
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


def test_winback_uses_observed_spend_rate():
    integrity, retention, operations = _build()
    h = compute_headlines(integrity, retention, operations)
    for c in h.tier_a_winnable:
        obs = _observed_monthly_spend(c)
        assert obs > 0, f"{c.customer_ref} has zero observed monthly spend"
        recoverable = obs * h.recovery_rate
        assert recoverable < obs, "Recoverable should be discounted below observed rate"
        assert recoverable > 0


def test_recovery_rate_configurable():
    integrity, retention, operations = _build()
    h50 = compute_headlines(integrity, retention, operations, recovery_rate=0.50)
    h30 = compute_headlines(integrity, retention, operations, recovery_rate=0.30)
    assert h50.monthly_winback_tier_a > h30.monthly_winback_tier_a
    assert h50.recovery_rate == 0.50
    assert h30.recovery_rate == 0.30


def test_sanity_bound_reported():
    integrity, retention, operations = _build()
    h = compute_headlines(integrity, retention, operations)
    assert h.winback_pct_of_revenue >= 0
    assert h.monthly_revenue > 0
    recomputed_pct = h.monthly_winback_total / h.monthly_revenue
    assert abs(recomputed_pct - h.winback_pct_of_revenue) < 0.001


def test_sanity_warning_triggers_at_high_recovery():
    integrity, retention, operations = _build()
    h = compute_headlines(integrity, retention, operations, recovery_rate=1.0)
    if h.winback_pct_of_revenue > SANITY_WINBACK_PCT_WARN:
        assert h.winback_sanity_warning
