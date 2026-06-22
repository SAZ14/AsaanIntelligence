"""Retention agent — finds regulars who are slipping away and estimates win-back value.

A customer is "lapsing" when the gap since their last visit is well beyond
their own normal visit cadence. Regulars who lapse (Tier A) are the highest-
value win-back targets. See README.md in this folder for the full method.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from app.core.models import MenuItem, Order, Staff


@dataclass
class CustomerProfile:
    customer_ref: str
    visit_count: int = 0
    first_visit: date | None = None
    last_visit: date | None = None
    total_spend: float = 0.0
    avg_ticket: float = 0.0
    mean_days_between: float | None = None
    median_cadence_days: float | None = None
    days_since_last: int = 0
    lapse_multiplier: float = 0.0
    is_lapsing: bool = False
    is_lapsed_regular: bool = False
    winback_value: float = 0.0


@dataclass
class RetentionReport:
    identified_orders: int = 0
    total_orders: int = 0
    identified_revenue: float = 0.0
    total_revenue: float = 0.0
    coverage_order_pct: float = 0.0
    coverage_revenue_pct: float = 0.0
    unique_customers: int = 0
    repeat_customers: int = 0
    repeat_rate: float = 0.0
    regular_count: int = 0
    cadence_threshold_days: float = 0.0
    lapsed_regular_count: int = 0
    lapsed_regular_winback: float = 0.0
    lapsing_count: int = 0
    lapsing_winback: float = 0.0
    total_winback_value: float = 0.0
    customers: list[CustomerProfile] = field(default_factory=list)


# ── Tunables ──

LAPSE_MULTIPLIER = 2.5
LAPSE_FLOOR_DAYS = 10
MIN_VISITS_FOR_CADENCE = 3


def _gaps(visit_dates: list[date]) -> list[int]:
    s = sorted(visit_dates)
    return [(s[i + 1] - s[i]).days for i in range(len(s) - 1)]


def _mean_gap(visit_dates: list[date]) -> float | None:
    g = _gaps(visit_dates)
    return sum(g) / len(g) if g else None


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def analyze_retention(
    orders: list[Order],
    menu: dict[str, MenuItem],
    staff: dict[str, Staff],
) -> RetentionReport:
    if not orders:
        return RetentionReport()

    all_dates = [o.datetime.date() for o in orders]
    period_end = max(all_dates)

    total_orders = len(orders)
    total_revenue = sum(p.amount for o in orders for p in o.payments)

    cust_visits: dict[str, list[date]] = defaultdict(list)
    cust_spend: dict[str, float] = defaultdict(float)

    identified_orders = 0
    identified_revenue = 0.0

    for o in orders:
        order_rev = sum(p.amount for p in o.payments)
        if o.customer_ref:
            identified_orders += 1
            identified_revenue += order_rev
            cust_visits[o.customer_ref].append(o.datetime.date())
            cust_spend[o.customer_ref] += order_rev

    profiles: list[CustomerProfile] = []

    for cref, dates in cust_visits.items():
        unique_dates = sorted(set(dates))
        vc = len(unique_dates)
        spend = cust_spend[cref]
        gaps = _gaps(unique_dates)
        median_cad = _median([float(g) for g in gaps]) if gaps else None
        days_since = (period_end - unique_dates[-1]).days

        profiles.append(CustomerProfile(
            customer_ref=cref,
            visit_count=vc,
            first_visit=unique_dates[0],
            last_visit=unique_dates[-1],
            total_spend=spend,
            avg_ticket=spend / vc if vc > 0 else 0.0,
            mean_days_between=_mean_gap(unique_dates),
            median_cadence_days=median_cad,
            days_since_last=days_since,
        ))

    repeat_count = sum(1 for p in profiles if p.visit_count > 1)
    unique_count = len(profiles)
    repeat_rate = repeat_count / unique_count if unique_count else 0.0

    # Cadence-based lapse detection
    for p in profiles:
        if p.visit_count < MIN_VISITS_FOR_CADENCE or p.median_cadence_days is None:
            continue
        cad = p.median_cadence_days
        threshold = max(LAPSE_FLOOR_DAYS, LAPSE_MULTIPLIER * cad)
        if cad > 0:
            p.lapse_multiplier = p.days_since_last / cad
        if p.days_since_last > threshold:
            p.is_lapsing = True
            p.winback_value = (p.days_since_last / cad) * p.avg_ticket

    # Derive "regular" cadence threshold from the data: customers whose
    # median cadence is in the tighter half (below median of all cadences).
    cadences = [p.median_cadence_days for p in profiles
                if p.median_cadence_days is not None and p.visit_count >= MIN_VISITS_FOR_CADENCE]
    cadence_threshold = _median(cadences) if cadences else 7.0

    regulars: list[CustomerProfile] = []
    for p in profiles:
        if (p.median_cadence_days is not None
                and p.median_cadence_days <= cadence_threshold
                and p.visit_count >= MIN_VISITS_FOR_CADENCE):
            regulars.append(p)
            if p.is_lapsing:
                p.is_lapsed_regular = True

    lapsed_regulars = [p for p in profiles if p.is_lapsed_regular]
    lapsing = [p for p in profiles if p.is_lapsing]

    lapsed_reg_wb = sum(p.winback_value for p in lapsed_regulars)
    lapsing_wb = sum(p.winback_value for p in lapsing)

    return RetentionReport(
        identified_orders=identified_orders,
        total_orders=total_orders,
        identified_revenue=identified_revenue,
        total_revenue=total_revenue,
        coverage_order_pct=identified_orders / total_orders if total_orders else 0.0,
        coverage_revenue_pct=identified_revenue / total_revenue if total_revenue else 0.0,
        unique_customers=unique_count,
        repeat_customers=repeat_count,
        repeat_rate=repeat_rate,
        regular_count=len(regulars),
        cadence_threshold_days=cadence_threshold,
        lapsed_regular_count=len(lapsed_regulars),
        lapsed_regular_winback=lapsed_reg_wb,
        lapsing_count=len(lapsing),
        lapsing_winback=lapsing_wb,
        total_winback_value=lapsing_wb,
        customers=sorted(profiles, key=lambda p: p.total_spend, reverse=True),
    )
