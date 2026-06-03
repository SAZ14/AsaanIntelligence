from pathlib import Path

from app.ingest import load_dataset
from app.analysis.integrity import analyze_integrity, IntegrityReport
from app.models.canonical import Order, LineItem, Payment, Staff, MenuItem

DATA = Path(__file__).resolve().parent.parent / "data"


def _load_report() -> IntegrityReport:
    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )
    return analyze_integrity(orders, menu, staff)


def test_worst_offender_is_s03():
    report = _load_report()
    assert report.worst_offender == "S03", f"Expected S03, got {report.worst_offender}"


def test_monthly_leakage_in_range():
    report = _load_report()
    lo, hi = 55_000, 65_000
    assert lo <= report.estimated_leakage_monthly <= hi, (
        f"Expected monthly leakage {lo}-{hi}, got {report.estimated_leakage_monthly:,.0f}"
    )


def test_theft_flags_concentrate_on_s03():
    report = _load_report()
    theft_events = [e for e in report.flagged_events if e.flag_type == "theft_void"]
    s03_theft = [e for e in theft_events if e.staff_id == "S03"]
    assert len(theft_events) > 0
    s03_value = sum(e.value for e in s03_theft)
    total_value = sum(e.value for e in theft_events)
    assert s03_value / total_value > 0.5, (
        f"S03 share of theft flags is only {s03_value / total_value:.1%}"
    )


def test_s03_has_lowest_integrity_score():
    report = _load_report()
    scores = {si.staff_id: si.integrity_score for si in report.staff_integrity}
    assert scores["S03"] == min(scores.values())


def test_venue_baseline_rates():
    report = _load_report()
    bl = report.venue_baseline
    assert bl.void_rate > 0
    assert bl.comp_rate > 0
    assert bl.discount_rate > 0
    assert bl.void_rate < 0.05
    assert bl.comp_rate < 0.05


def test_no_false_positives_on_clean_data():
    """With no planted bad actor, leakage should be near zero."""
    from datetime import datetime

    staff_clean = {
        "S01": Staff(staff_id="S01", name="Alice", role="barista"),
        "S02": Staff(staff_id="S02", name="Bob", role="server"),
    }
    menu_clean = {
        "ESP": MenuItem(sku="ESP", name="Espresso", category="Coffee", cost=70, price=420),
        "LAT": MenuItem(sku="LAT", name="Latte", category="Coffee", cost=120, price=580),
    }

    orders_clean: list[Order] = []
    for i in range(500):
        sid = "S01" if i % 2 == 0 else "S02"
        orders_clean.append(Order(
            order_id=f"ORD{i:05d}",
            datetime=datetime(2026, 5, 1, 10, i % 60),
            staff_id=sid,
            staff_name=staff_clean[sid].name,
            channel="dine_in",
            order_status="closed",
            line_items=[
                LineItem(
                    item_sku="ESP",
                    item_name="Espresso",
                    category="Coffee",
                    qty=1,
                    unit_price=420,
                    line_amount=420,
                ),
            ],
            payments=[Payment(method="card", amount=441, tax_rate=0.05)],
        ))

    report = analyze_integrity(orders_clean, menu_clean, staff_clean)
    assert report.estimated_leakage_period == 0.0, (
        f"Expected zero leakage on clean data, got {report.estimated_leakage_period:,.0f}"
    )
    for si in report.staff_integrity:
        assert si.integrity_score >= 99.0, (
            f"Staff {si.staff_id} flagged on clean data: score={si.integrity_score}"
        )
