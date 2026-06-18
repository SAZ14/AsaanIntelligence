from datetime import datetime
from pathlib import Path

from app.ingest import load_dataset
from app.analysis.reconciliation import reconcile_payments
from app.models.canonical import LineItem, MenuItem, Order, Payment, Staff

DATA = Path(__file__).resolve().parent.parent / "data"


def _load():
    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )
    return reconcile_payments(orders, menu, staff)


def test_money_flow_consistent():
    r = _load()
    assert 4_400_000 <= r.net_sales <= 4_700_000
    assert 4_800_000 <= r.gross_collected <= 5_200_000
    # gross collected = net sales + tax collected
    assert abs(r.gross_collected - (r.net_sales + r.tax_collected)) < 1.0


def test_profit_math():
    r = _load()
    assert abs(r.gross_profit - (r.net_sales - r.cogs_sold)) < 1e-6
    assert 0.55 <= r.gross_margin <= 0.80
    assert r.cogs_sold > 0
    assert r.wasted_cogs > 0  # comps + fired-then-voided cost real money


def test_clean_synthetic_books_balance():
    """The synthetic export is internally consistent: payments reconcile."""
    r = _load()
    assert r.payment_mismatch_count == 0
    assert abs(r.net_unreconciled) < 1.0
    assert r.tax_anomaly_count == 0
    assert r.books_balanced is True


def test_method_shares_sum_to_one():
    r = _load()
    total = sum(m.share_pct for m in r.by_method)
    assert abs(total - 1.0) < 1e-6
    assert {"cash", "card", "wallet", "qr"} <= {m.method for m in r.by_method}


def _menu():
    return {"ESP": MenuItem(sku="ESP", name="Espresso", category="Coffee", cost=70, price=420)}


def _staff():
    return {"S01": Staff(staff_id="S01", name="Alice", role="barista")}


def _order(oid, lines, method, amount, tax):
    return Order(
        order_id=oid, datetime=datetime(2026, 5, 1, 10, 0), staff_id="S01",
        staff_name="Alice", channel="dine_in", order_status="closed",
        line_items=lines, payments=[Payment(method=method, amount=amount, tax_rate=tax)],
    )


def test_payment_shortfall_detected():
    line = LineItem(item_sku="ESP", item_name="Espresso", category="Coffee",
                    qty=1, unit_price=420, line_amount=420)
    # expected gross = 420 * 1.15 = 483; collected only 400 -> shortfall
    r = reconcile_payments([_order("O1", [line], "cash", 400.0, 0.15)], _menu(), _staff())
    assert r.payment_mismatch_count == 1
    assert r.books_balanced is False
    d = r.discrepancies[0]
    assert d.kind == "payment_shortfall"
    assert d.delta < 0


def test_tax_anomaly_detected():
    line = LineItem(item_sku="ESP", item_name="Espresso", category="Coffee",
                    qty=1, unit_price=420, line_amount=420)
    # cash order taxed at digital 5% instead of 15%; collected matches the wrong rate
    r = reconcile_payments([_order("O2", [line], "cash", 441.0, 0.05)], _menu(), _staff())
    assert r.tax_anomaly_count == 1
    assert any(d.kind == "tax_anomaly" for d in r.discrepancies)


def test_empty_orders():
    r = reconcile_payments([], _menu(), _staff())
    assert r.total_orders == 0
    assert r.books_balanced is True
