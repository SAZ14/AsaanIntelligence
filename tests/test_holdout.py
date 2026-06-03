"""Held-out generalisation test: different offender (S06), different seed (99)."""

from pathlib import Path

from app.ingest import load_dataset
from app.analysis.integrity import analyze_integrity

HOLDOUT = Path(__file__).resolve().parent.parent / "data" / "holdout"


def _load_holdout_report():
    orders, menu, staff = load_dataset(
        HOLDOUT / "sales_detail.csv", HOLDOUT / "menu.csv", HOLDOUT / "staff.csv",
    )
    return analyze_integrity(orders, menu, staff)


def test_detects_s06_as_worst_offender():
    report = _load_holdout_report()
    assert report.worst_offender == "S06", f"Expected S06, got {report.worst_offender}"


def test_holdout_leakage_reconciles():
    report = _load_holdout_report()
    component_sum = (
        report.suspected_theft_value
        + report.excess_comp_value
        + report.excess_discount_value
    )
    assert component_sum == report.estimated_leakage_period


def test_holdout_s06_dominates_leakage():
    report = _load_holdout_report()
    s06 = next(si for si in report.staff_integrity if si.staff_id == "S06")
    assert s06.total_leakage / report.estimated_leakage_period > 0.8


def test_holdout_honest_staff_not_flagged():
    report = _load_holdout_report()
    for si in report.staff_integrity:
        if si.staff_id != "S06":
            assert si.integrity_score >= 99.0, (
                f"Honest staff {si.staff_id} flagged: score={si.integrity_score:.1f}"
            )
