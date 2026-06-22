from pathlib import Path

from app.core.ingest import load_dataset

DATA = Path(__file__).resolve().parent.parent / "data"


def _load():
    return load_dataset(DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv")


def test_order_and_line_counts():
    orders, menu, staff = _load()
    total_lines = sum(len(o.line_items) for o in orders)
    assert 3700 <= len(orders) <= 3900, f"Expected ~3795 orders, got {len(orders)}"
    assert 5900 <= total_lines <= 6100, f"Expected ~5986 lines, got {total_lines}"


def test_revenue_range():
    orders, _, _ = _load()
    revenue = sum(p.amount for o in orders for p in o.payments)
    assert 4_800_000 <= revenue <= 5_200_000, f"Expected ~4.97M, got {revenue:,.0f}"


def test_void_and_comp_counts():
    orders, _, _ = _load()
    voids = sum(1 for o in orders for li in o.line_items if li.is_void)
    comps = sum(1 for o in orders for li in o.line_items if li.is_comp)
    assert 100 <= voids <= 150, f"Expected ~122 voids, got {voids}"
    assert 100 <= comps <= 140, f"Expected ~118 comps, got {comps}"


def test_unique_customers():
    orders, _, _ = _load()
    custs = {o.customer_ref for o in orders if o.customer_ref}
    assert 680 <= len(custs) <= 740, f"Expected ~710 customers, got {len(custs)}"


def test_menu_margins():
    _, menu, _ = _load()
    assert menu["IMP"].margin is not None
    assert menu["IMP"].margin < 0.25
    assert menu["ESP"].margin is not None
    assert menu["ESP"].margin > 0.75


def test_digital_share():
    orders, _, _ = _load()
    digital = sum(1 for o in orders if o.payments[0].method in ("card", "wallet", "qr"))
    pct = digital / len(orders) * 100
    assert 55 <= pct <= 65, f"Expected ~60% digital, got {pct:.1f}%"
