"""Payment ↔ sales reconciliation, profit, and discrepancy detection.

Where ``integrity.py`` looks for *behavioural* leakage (a staff member voiding
fired cash items, over-comping, over-discounting), this module does the
*arithmetic* audit that a POS should always pass:

  * **Payment reconciliation** — for every order, does the money collected equal
    what the till says was owed (net sales + tax)? Mismatches are cash
    shrinkage, mis-rings, or recording errors.
  * **Profit** — real gross profit from menu cost (COGS), plus the cost burned
    making items that were comped or fired-then-voided (wasted COGS).
  * **Tax integrity** — the cash-vs-digital tax lever (15% cash / 5% digital);
    an order taxed at the wrong rate is flagged.

It is fully deterministic so the figures the LLM agent reasons over are exact.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.models.canonical import MenuItem, Order, Staff

# Money is compared to this tolerance (PKR) before calling it a discrepancy.
AMOUNT_TOLERANCE = 0.5

# The ICT tax lever: what each payment method *should* be taxed at.
EXPECTED_TAX_RATES = {"cash": 0.15, "card": 0.05, "wallet": 0.05, "qr": 0.05}
TAX_TOLERANCE = 1e-6


@dataclass
class Discrepancy:
    order_id: str
    staff_id: str
    staff_name: str
    kind: str  # "payment_shortfall", "payment_overage", "tax_anomaly"
    expected: float
    actual: float
    delta: float
    detail: str = ""


@dataclass
class MethodBreakdown:
    method: str
    orders: int = 0
    net_sales: float = 0.0
    tax_collected: float = 0.0
    gross_collected: float = 0.0
    share_pct: float = 0.0


@dataclass
class StaffReconciliation:
    staff_id: str
    staff_name: str
    orders: int = 0
    mismatch_count: int = 0
    mismatch_abs_value: float = 0.0
    net_shortfall: float = 0.0  # collected minus expected, summed (signed)
    tax_anomaly_count: int = 0


@dataclass
class ReconciliationReport:
    period_days: int = 0
    total_orders: int = 0
    total_lines: int = 0

    # Money flow (all ex-tax unless named "gross"/"tax")
    net_sales: float = 0.0
    total_discounts: float = 0.0
    tax_collected: float = 0.0
    gross_collected: float = 0.0

    # Profit
    cogs_sold: float = 0.0
    gross_profit: float = 0.0
    gross_margin: float = 0.0
    wasted_cogs: float = 0.0
    comp_retail_value: float = 0.0
    void_retail_value: float = 0.0
    lines_missing_cost: int = 0

    # Discrepancies
    payment_mismatch_count: int = 0
    payment_mismatch_abs_value: float = 0.0
    net_unreconciled: float = 0.0  # signed sum of (collected - expected)
    tax_anomaly_count: int = 0
    tax_anomaly_value: float = 0.0
    books_balanced: bool = True

    discrepancies: list[Discrepancy] = field(default_factory=list)
    by_method: list[MethodBreakdown] = field(default_factory=list)
    by_staff: list[StaffReconciliation] = field(default_factory=list)


def _line_retail(li) -> float:
    return li.unit_price * li.qty


def _line_cost(li, menu: dict[str, MenuItem]) -> float | None:
    item = menu.get(li.item_sku)
    if item is None or item.cost is None:
        return None
    return item.cost * li.qty


def reconcile_payments(
    orders: list[Order],
    menu: dict[str, MenuItem],
    staff: dict[str, Staff],
) -> ReconciliationReport:
    if not orders:
        return ReconciliationReport()

    timestamps = [o.datetime for o in orders]
    period_days = max(1, (max(timestamps).date() - min(timestamps).date()).days + 1)

    report = ReconciliationReport(period_days=period_days, total_orders=len(orders))
    methods: dict[str, MethodBreakdown] = {}
    per_staff: dict[str, StaffReconciliation] = {}
    for sid, s in staff.items():
        per_staff[sid] = StaffReconciliation(staff_id=sid, staff_name=s.name)

    for order in orders:
        sid = order.staff_id
        if sid not in per_staff:
            per_staff[sid] = StaffReconciliation(staff_id=sid, staff_name=order.staff_name)
        sr = per_staff[sid]
        sr.orders += 1

        payment = order.payments[0] if order.payments else None
        method = payment.method if payment else "unknown"
        tax_rate = payment.tax_rate if payment else 0.0
        collected = payment.amount if payment else 0.0

        order_net = 0.0
        for li in order.line_items:
            report.total_lines += 1
            line_cost = _line_cost(li, menu)
            counts_cost = line_cost is not None
            if not counts_cost:
                report.lines_missing_cost += 1

            if li.is_void:
                report.void_retail_value += _line_retail(li)
                # Fired-then-voided items were actually made → cost is sunk.
                if li.void_after_fire and counts_cost:
                    report.wasted_cogs += line_cost
                continue
            if li.is_comp:
                report.comp_retail_value += _line_retail(li)
                if counts_cost:
                    report.wasted_cogs += line_cost
                continue

            # Active (sold) line.
            order_net += li.line_amount
            report.net_sales += li.line_amount
            report.total_discounts += li.discount_amount
            if counts_cost:
                report.cogs_sold += line_cost

        # ── Payment reconciliation ──
        expected_gross = round(order_net * (1 + tax_rate), 2)
        delta = round(collected - expected_gross, 2)
        order_tax = round(collected - order_net, 2) if collected else round(order_net * tax_rate, 2)

        mb = methods.setdefault(method, MethodBreakdown(method=method))
        mb.orders += 1
        mb.net_sales += order_net
        mb.gross_collected += collected
        mb.tax_collected += max(0.0, collected - order_net)

        report.gross_collected += collected
        report.tax_collected += max(0.0, collected - order_net)

        if abs(delta) > AMOUNT_TOLERANCE:
            kind = "payment_shortfall" if delta < 0 else "payment_overage"
            report.payment_mismatch_count += 1
            report.payment_mismatch_abs_value += abs(delta)
            report.net_unreconciled += delta
            sr.mismatch_count += 1
            sr.mismatch_abs_value += abs(delta)
            sr.net_shortfall += delta
            report.discrepancies.append(Discrepancy(
                order_id=order.order_id,
                staff_id=sid,
                staff_name=order.staff_name,
                kind=kind,
                expected=expected_gross,
                actual=collected,
                delta=delta,
                detail=f"{method}: collected {collected:,.0f} vs expected {expected_gross:,.0f}",
            ))

        # ── Tax integrity ──
        expected_rate = EXPECTED_TAX_RATES.get(method)
        if expected_rate is not None and abs(tax_rate - expected_rate) > TAX_TOLERANCE:
            impact = round(order_net * abs(expected_rate - tax_rate), 2)
            report.tax_anomaly_count += 1
            report.tax_anomaly_value += impact
            sr.tax_anomaly_count += 1
            report.discrepancies.append(Discrepancy(
                order_id=order.order_id,
                staff_id=sid,
                staff_name=order.staff_name,
                kind="tax_anomaly",
                expected=expected_rate,
                actual=tax_rate,
                delta=round(tax_rate - expected_rate, 4),
                detail=f"{method} taxed at {tax_rate:.0%}, expected {expected_rate:.0%} "
                       f"(≈ PKR {impact:,.0f} impact)",
            ))

    # ── Roll-ups ──
    report.gross_profit = report.net_sales - report.cogs_sold
    report.gross_margin = report.gross_profit / report.net_sales if report.net_sales > 0 else 0.0
    report.books_balanced = (
        report.payment_mismatch_count == 0 and report.tax_anomaly_count == 0
    )

    total_gross = report.gross_collected
    for mb in methods.values():
        mb.share_pct = mb.gross_collected / total_gross if total_gross > 0 else 0.0
    report.by_method = sorted(methods.values(), key=lambda m: m.gross_collected, reverse=True)

    report.by_staff = sorted(
        [s for s in per_staff.values() if s.orders > 0],
        key=lambda s: (s.mismatch_abs_value, s.tax_anomaly_count),
        reverse=True,
    )

    report.discrepancies.sort(key=lambda d: abs(d.delta) if d.kind != "tax_anomaly" else 0, reverse=True)
    return report

