from datetime import datetime, date, timedelta
from pathlib import Path

from app.ingest import load_dataset
from app.models.canonical import Order, LineItem, Payment, Staff, MenuItem
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
    lapsed_regs = [c for c in ret.customers if c.is_lapsed_regular]
    assert ret.lapsed_regular_count > 0
    assert ret.lapsed_regular_count == len(lapsed_regs)
    for c in lapsed_regs:
        assert c.winback_value > 0
        assert c.visit_count >= 3
        assert c.median_cadence_days is not None
        assert c.days_since_last > 0


def test_lapsing_superset_of_lapsed_regulars():
    orders, menu, staff = _load()
    ret = analyze_retention(orders, menu, staff)
    assert ret.lapsing_count >= ret.lapsed_regular_count
    assert ret.lapsing_winback >= ret.lapsed_regular_winback


def test_tight_cadence_long_gap_flagged():
    """A customer visiting every 3 days who disappears for 20 days is lapsing."""
    staff_t = {"S01": Staff(staff_id="S01", name="A", role="server")}
    menu_t = {"ESP": MenuItem(sku="ESP", name="Espresso", category="Coffee", cost=70, price=420)}
    orders_t: list[Order] = []
    base = date(2026, 5, 1)
    visit_days = [0, 3, 6, 9, 12]  # cadence = 3 days, last visit day 12
    period_end_day = 35
    for i, d in enumerate(visit_days):
        dt = datetime.combine(base + timedelta(days=d), datetime.min.time().replace(hour=10))
        orders_t.append(Order(
            order_id=f"O{i:04d}", datetime=dt,
            staff_id="S01", staff_name="A", channel="dine_in",
            order_status="closed", customer_ref="CUST_TIGHT",
            line_items=[LineItem(item_sku="ESP", item_name="Espresso",
                                 category="Coffee", qty=1, unit_price=420, line_amount=420)],
            payments=[Payment(method="card", amount=441, tax_rate=0.05)],
        ))
    # pad with other orders to set period_end
    end_dt = datetime.combine(base + timedelta(days=period_end_day), datetime.min.time().replace(hour=10))
    orders_t.append(Order(
        order_id="OPAD", datetime=end_dt,
        staff_id="S01", staff_name="A", channel="dine_in",
        order_status="closed", customer_ref="CUST_ACTIVE",
        line_items=[LineItem(item_sku="ESP", item_name="Espresso",
                             category="Coffee", qty=1, unit_price=420, line_amount=420)],
        payments=[Payment(method="card", amount=441, tax_rate=0.05)],
    ))
    ret = analyze_retention(orders_t, menu_t, staff_t)
    tight = next(c for c in ret.customers if c.customer_ref == "CUST_TIGHT")
    assert tight.is_lapsing, "Tight-cadence customer with long gap should be lapsing"
    assert tight.winback_value > 0


def test_active_customer_not_flagged():
    """A customer still visiting at their normal cadence is NOT lapsing."""
    staff_t = {"S01": Staff(staff_id="S01", name="A", role="server")}
    menu_t = {"ESP": MenuItem(sku="ESP", name="Espresso", category="Coffee", cost=70, price=420)}
    orders_t: list[Order] = []
    base = date(2026, 5, 1)
    visit_days = [0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 30]
    for i, d in enumerate(visit_days):
        dt = datetime.combine(base + timedelta(days=d), datetime.min.time().replace(hour=10))
        orders_t.append(Order(
            order_id=f"O{i:04d}", datetime=dt,
            staff_id="S01", staff_name="A", channel="dine_in",
            order_status="closed", customer_ref="CUST_STEADY",
            line_items=[LineItem(item_sku="ESP", item_name="Espresso",
                                 category="Coffee", qty=1, unit_price=420, line_amount=420)],
            payments=[Payment(method="card", amount=441, tax_rate=0.05)],
        ))
    ret = analyze_retention(orders_t, menu_t, staff_t)
    steady = next(c for c in ret.customers if c.customer_ref == "CUST_STEADY")
    assert not steady.is_lapsing, "Customer visiting at cadence should not be lapsing"


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


def test_regulars_derived_from_cadence():
    orders, menu, staff = _load()
    ret = analyze_retention(orders, menu, staff)
    assert ret.cadence_threshold_days > 0
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
            assert lowest.margin <= mi.margin


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
    assert ret.lapsing_count > 0
    assert ret.lapsed_regular_count > 0


def test_holdout_operations_computes():
    orders, menu, staff = _load(HOLDOUT)
    ops = analyze_operations(orders, menu, staff)
    assert ops.avg_ticket > 0
    assert len(ops.items_by_volume) > 0
    assert len(ops.items_by_margin) > 0
    lowest = ops.items_by_margin[0]
    assert lowest.sku == "IMP"
