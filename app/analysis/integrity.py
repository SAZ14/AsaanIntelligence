from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from app.models.canonical import MenuItem, Order, Staff


@dataclass
class FlaggedEvent:
    order_id: str
    staff_id: str
    staff_name: str
    flag_type: str  # "theft_void", "excess_comp", "excess_discount"
    item_sku: str
    item_name: str
    value: float


@dataclass
class StaffIntegrity:
    staff_id: str
    staff_name: str
    total_lines: int = 0
    gross_volume: float = 0.0
    void_count: int = 0
    void_value: float = 0.0
    void_rate: float = 0.0
    comp_count: int = 0
    comp_value: float = 0.0
    comp_rate: float = 0.0
    discount_value: float = 0.0
    discount_rate: float = 0.0
    cash_orders: int = 0
    total_orders: int = 0
    cash_share: float = 0.0
    theft_void_count: int = 0
    theft_void_value: float = 0.0
    excess_theft_void_value: float = 0.0
    excess_comp_value: float = 0.0
    excess_discount_value: float = 0.0
    total_leakage: float = 0.0
    integrity_score: float = 100.0
    void_rate_z: float = 0.0
    comp_rate_z: float = 0.0
    discount_rate_z: float = 0.0


@dataclass
class VenueBaseline:
    total_orders: int = 0
    total_lines: int = 0
    gross_volume: float = 0.0
    void_count: int = 0
    void_value: float = 0.0
    void_rate: float = 0.0
    comp_count: int = 0
    comp_value: float = 0.0
    comp_rate: float = 0.0
    discount_value: float = 0.0
    discount_rate: float = 0.0
    theft_void_value: float = 0.0
    theft_void_rate: float = 0.0
    period_days: int = 0


@dataclass
class IntegrityReport:
    venue_baseline: VenueBaseline
    staff_integrity: list[StaffIntegrity] = field(default_factory=list)
    worst_offender: str = ""
    suspected_theft_value: float = 0.0
    excess_comp_value: float = 0.0
    excess_discount_value: float = 0.0
    estimated_leakage_period: float = 0.0
    estimated_leakage_monthly: float = 0.0
    flagged_events: list[FlaggedEvent] = field(default_factory=list)


def _line_gross(li) -> float:
    return li.unit_price * li.qty


def _z_score(value: float, values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    std = math.sqrt(var)
    if std < 1e-9:
        return 0.0
    return (value - mean) / std


def analyze_integrity(
    orders: list[Order],
    menu: dict[str, MenuItem],
    staff: dict[str, Staff],
) -> IntegrityReport:
    if not orders:
        return IntegrityReport(venue_baseline=VenueBaseline())

    timestamps = [o.datetime for o in orders]
    period_days = max(1, (max(timestamps).date() - min(timestamps).date()).days + 1)

    per_staff: dict[str, StaffIntegrity] = {}
    for sid, s in staff.items():
        per_staff[sid] = StaffIntegrity(staff_id=sid, staff_name=s.name)

    baseline = VenueBaseline(period_days=period_days)
    flagged: list[FlaggedEvent] = []

    for order in orders:
        sid = order.staff_id
        if sid not in per_staff:
            per_staff[sid] = StaffIntegrity(staff_id=sid, staff_name=order.staff_name)
        si = per_staff[sid]

        is_cash = order.payments[0].method == "cash" if order.payments else False
        si.total_orders += 1
        if is_cash:
            si.cash_orders += 1

        baseline.total_orders += 1

        for li in order.line_items:
            gross = _line_gross(li)
            baseline.total_lines += 1
            baseline.gross_volume += gross
            si.total_lines += 1
            si.gross_volume += gross

            if li.is_void:
                baseline.void_count += 1
                baseline.void_value += gross
                si.void_count += 1
                si.void_value += gross

                if li.void_after_fire and is_cash:
                    si.theft_void_count += 1
                    si.theft_void_value += gross
                    baseline.theft_void_value += gross
                    flagged.append(FlaggedEvent(
                        order_id=order.order_id,
                        staff_id=sid,
                        staff_name=order.staff_name,
                        flag_type="theft_void",
                        item_sku=li.item_sku,
                        item_name=li.item_name,
                        value=gross,
                    ))

            elif li.is_comp:
                baseline.comp_count += 1
                baseline.comp_value += gross
                si.comp_count += 1
                si.comp_value += gross

            if not li.is_void and not li.is_comp and li.discount_amount > 0:
                baseline.discount_value += li.discount_amount
                si.discount_value += li.discount_amount

    if baseline.gross_volume > 0:
        baseline.void_rate = baseline.void_value / baseline.gross_volume
        baseline.comp_rate = baseline.comp_value / baseline.gross_volume
        baseline.discount_rate = baseline.discount_value / baseline.gross_volume
        baseline.theft_void_rate = baseline.theft_void_value / baseline.gross_volume

    for si in per_staff.values():
        if si.gross_volume > 0:
            si.void_rate = si.void_value / si.gross_volume
            si.comp_rate = si.comp_value / si.gross_volume
            si.discount_rate = si.discount_value / si.gross_volume
        if si.total_orders > 0:
            si.cash_share = si.cash_orders / si.total_orders

        si.excess_theft_void_value = max(0.0, si.theft_void_value - baseline.theft_void_rate * si.gross_volume)
        si.excess_comp_value = max(0.0, si.comp_value - baseline.comp_rate * si.gross_volume)
        si.excess_discount_value = max(0.0, si.discount_value - baseline.discount_rate * si.gross_volume)
        si.total_leakage = si.excess_theft_void_value + si.excess_comp_value + si.excess_discount_value

    staff_list = [si for si in per_staff.values() if si.total_lines > 0]

    void_rates = [si.void_rate for si in staff_list]
    comp_rates = [si.comp_rate for si in staff_list]
    disc_rates = [si.discount_rate for si in staff_list]

    for si in staff_list:
        si.void_rate_z = _z_score(si.void_rate, void_rates)
        si.comp_rate_z = _z_score(si.comp_rate, comp_rates)
        si.discount_rate_z = _z_score(si.discount_rate, disc_rates)

        penalty = (
            si.excess_theft_void_value * 3.0
            + si.excess_comp_value * 2.0
            + si.excess_discount_value * 1.0
        )
        volume_norm = si.gross_volume if si.gross_volume > 0 else 1.0
        si.integrity_score = max(0.0, 100.0 - (penalty / volume_norm) * 100.0)

    staff_list.sort(key=lambda s: s.integrity_score)

    total_excess_theft = sum(si.excess_theft_void_value for si in staff_list)
    total_excess_comp = sum(si.excess_comp_value for si in staff_list)
    total_excess_disc = sum(si.excess_discount_value for si in staff_list)
    total_leakage = total_excess_theft + total_excess_comp + total_excess_disc

    return IntegrityReport(
        venue_baseline=baseline,
        staff_integrity=staff_list,
        worst_offender=staff_list[0].staff_id if staff_list else "",
        suspected_theft_value=total_excess_theft,
        excess_comp_value=total_excess_comp,
        excess_discount_value=total_excess_disc,
        estimated_leakage_period=total_leakage,
        estimated_leakage_monthly=total_leakage * 30 / period_days,
        flagged_events=sorted(flagged, key=lambda e: e.value, reverse=True),
    )
