from pathlib import Path

from app.ingest import load_dataset
from app.analysis.retention import analyze_retention, analyze_operations

DATA = Path(__file__).resolve().parent.parent / "data"
HOLDOUT = DATA / "holdout"


def _load(data_dir=DATA):
    return load_dataset(
        data_dir / "sales_detail.csv", data_dir / "menu.csv", data_dir / "staff.csv",
    )


# ── Retention tests ──


def test_lapsed_regulars_detected():
    orders, menu, staff = _load()
    ret = analyze_retention(orders, menu, staff)
    lapsed = [c for c in ret.customers if c.is_lapsed]
    assert len(lapsed) > 0, "No lapsed regulars detected"
    for c in lapsed:
        assert c.winback_value > 0, f"Lapsed {c.customer_ref} has no winback value"
        assert c.visit_count >= 2, f"Lapsed {c.customer_ref} has only {c.visit_count} visits"


def test_repeat_rate_valid():
    orders, menu, staff = _load()
    ret = analyze_retention(orders, menu, staff)
    assert 0.0 <= ret.repeat_rate <= 1.0


def test_coverage_reported():
    orders, menu, staff = _load()
    ret = analyze_retention(orders, menu, staff)
    assert 0.0 < ret.coverage_order_pct < 1.0
    assert 0.0 < ret.coverage_revenue_pct < 1.0
    assert ret.identified_orders > 0
    assert ret.total_orders > ret.identified_orders


def test_regulars_threshold_derived():
    orders, menu, staff = _load()
    ret = analyze_retention(orders, menu, staff)
    assert ret.regular_threshold_visits > 1
    assert ret.regular_count > 0
    assert ret.regular_count < ret.unique_customers


# ── Operations tests ──


def test_afternoon_is_deadest_core_daypart():
    orders, menu, staff = _load()
    ops = analyze_operations(orders, menu, staff)
    core = [dp for dp in ops.dayparts if dp.name in ("Morning", "Midday", "Afternoon", "Evening")]
    deadest_core = min(core, key=lambda d: d.order_count)
    assert deadest_core.name == "Afternoon"


def test_busiest_dayparts_are_morning_or_evening():
    orders, menu, staff = _load()
    ops = analyze_operations(orders, menu, staff)
    assert ops.busiest_daypart in ("Morning", "Evening")


def test_lowest_margin_item():
    orders, menu, staff = _load()
    ops = analyze_operations(orders, menu, staff)
    assert len(ops.items_by_margin) > 0
    lowest = ops.items_by_margin[0]
    for mi in menu.values():
        if mi.margin is not None:
            assert lowest.margin <= mi.margin, (
                f"{lowest.sku} margin {lowest.margin} is not the lowest; "
                f"{mi.sku} has {mi.margin}"
            )


def test_digital_share_reported():
    orders, menu, staff = _load()
    ops = analyze_operations(orders, menu, staff)
    methods = {ps.method: ps for ps in ops.payment_shares}
    assert "cash" in methods
    digital = sum(ps.share_pct for ps in ops.payment_shares if ps.method != "cash")
    assert digital > 0.5


def test_channels_reported():
    orders, menu, staff = _load()
    ops = analyze_operations(orders, menu, staff)
    assert len(ops.channels) >= 2
    for ch in ops.channels:
        assert ch.avg_ticket > 0


# ── Holdout generalisation ──


def test_holdout_retention_computes():
    orders, menu, staff = _load(HOLDOUT)
    ret = analyze_retention(orders, menu, staff)
    assert ret.total_orders > 0
    assert ret.unique_customers > 0
    assert 0.0 <= ret.repeat_rate <= 1.0
    assert ret.coverage_order_pct > 0


def test_holdout_operations_computes():
    orders, menu, staff = _load(HOLDOUT)
    ops = analyze_operations(orders, menu, staff)
    assert ops.avg_ticket > 0
    assert len(ops.items_by_volume) > 0
    assert len(ops.items_by_margin) > 0
    lowest = ops.items_by_margin[0]
    assert lowest.sku == "IMP"
