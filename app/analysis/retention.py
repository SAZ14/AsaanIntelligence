from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.models.canonical import MenuItem, Order, Staff


# ── Retention dataclasses ──


@dataclass
class CustomerProfile:
    customer_ref: str
    visit_count: int = 0
    first_visit: date | None = None
    last_visit: date | None = None
    total_spend: float = 0.0
    avg_ticket: float = 0.0
    mean_days_between: float | None = None
    is_regular: bool = False
    is_lapsed: bool = False
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
    regular_threshold_visits: float = 0.0
    lapsed_regular_count: int = 0
    total_winback_value: float = 0.0
    lapse_window_days: int = 14
    customers: list[CustomerProfile] = field(default_factory=list)


# ── Operations dataclasses ──


@dataclass
class HourStats:
    hour: int
    order_count: int = 0
    revenue: float = 0.0


@dataclass
class DayOfWeekStats:
    day: int = 0
    day_name: str = ""
    order_count: int = 0
    revenue: float = 0.0
    num_occurrences: int = 0
    avg_orders: float = 0.0
    avg_revenue: float = 0.0


@dataclass
class DaypartStats:
    name: str = ""
    start_hour: int = 0
    end_hour: int = 0
    order_count: int = 0
    revenue: float = 0.0
    avg_ticket: float = 0.0


@dataclass
class ChannelStats:
    channel: str = ""
    order_count: int = 0
    revenue: float = 0.0
    avg_ticket: float = 0.0


@dataclass
class PaymentShareStats:
    method: str = ""
    order_count: int = 0
    share_pct: float = 0.0
    revenue: float = 0.0


@dataclass
class ItemRank:
    sku: str
    name: str
    category: str
    volume: int = 0
    revenue: float = 0.0
    margin: float | None = None
    margin_contribution: float = 0.0


@dataclass
class OperationsReport:
    avg_ticket: float = 0.0
    hours: list[HourStats] = field(default_factory=list)
    days_of_week: list[DayOfWeekStats] = field(default_factory=list)
    dayparts: list[DaypartStats] = field(default_factory=list)
    channels: list[ChannelStats] = field(default_factory=list)
    payment_shares: list[PaymentShareStats] = field(default_factory=list)
    items_by_volume: list[ItemRank] = field(default_factory=list)
    items_by_margin: list[ItemRank] = field(default_factory=list)
    busiest_hour: int = 0
    deadest_hour: int = 0
    busiest_day: str = ""
    deadest_day: str = ""
    busiest_daypart: str = ""
    deadest_daypart: str = ""


# ── Retention analysis ──


def _mean_gap(visit_dates: list[date]) -> float | None:
    if len(visit_dates) < 2:
        return None
    sorted_d = sorted(visit_dates)
    gaps = [(sorted_d[i + 1] - sorted_d[i]).days for i in range(len(sorted_d) - 1)]
    return sum(gaps) / len(gaps)


def analyze_retention(
    orders: list[Order],
    menu: dict[str, MenuItem],
    staff: dict[str, Staff],
    lapse_window_days: int = 14,
) -> RetentionReport:
    if not orders:
        return RetentionReport()

    all_dates = [o.datetime.date() for o in orders]
    period_end = max(all_dates)
    lapse_cutoff = period_end - timedelta(days=lapse_window_days - 1)

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
    visit_counts: list[int] = []

    for cref, dates in cust_visits.items():
        unique_dates = sorted(set(dates))
        vc = len(unique_dates)
        visit_counts.append(vc)
        spend = cust_spend[cref]
        profiles.append(CustomerProfile(
            customer_ref=cref,
            visit_count=vc,
            first_visit=unique_dates[0],
            last_visit=unique_dates[-1],
            total_spend=spend,
            avg_ticket=spend / vc if vc > 0 else 0.0,
            mean_days_between=_mean_gap(unique_dates),
        ))

    repeat_count = sum(1 for p in profiles if p.visit_count > 1)
    unique_count = len(profiles)
    repeat_rate = repeat_count / unique_count if unique_count else 0.0

    if len(visit_counts) >= 2:
        mean_vc = sum(visit_counts) / len(visit_counts)
        std_vc = math.sqrt(sum((v - mean_vc) ** 2 for v in visit_counts) / len(visit_counts))
        threshold = mean_vc + std_vc
    else:
        threshold = 2.0

    for p in profiles:
        if p.visit_count >= threshold:
            p.is_regular = True

    for p in profiles:
        if not p.is_regular:
            continue
        early_visits = [d for d in cust_visits[p.customer_ref] if d < lapse_cutoff]
        recent_visits = [d for d in cust_visits[p.customer_ref] if d >= lapse_cutoff]
        if len(early_visits) >= 2 and len(recent_visits) == 0:
            p.is_lapsed = True
            early_unique = sorted(set(early_visits))
            if len(early_unique) >= 2:
                span = (early_unique[-1] - early_unique[0]).days
                if span > 0:
                    freq = len(early_unique) / span
                    p.winback_value = freq * p.avg_ticket * lapse_window_days

    lapsed = [p for p in profiles if p.is_lapsed]
    regulars = [p for p in profiles if p.is_regular]

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
        regular_threshold_visits=threshold,
        lapsed_regular_count=len(lapsed),
        total_winback_value=sum(p.winback_value for p in lapsed),
        lapse_window_days=lapse_window_days,
        customers=sorted(profiles, key=lambda p: p.total_spend, reverse=True),
    )


# ── Operations analysis ──


DAYPARTS = [
    ("Early morning", 5, 7),
    ("Morning", 7, 11),
    ("Midday", 11, 14),
    ("Afternoon", 14, 17),
    ("Evening", 17, 21),
    ("Late night", 21, 24),
]

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def analyze_operations(
    orders: list[Order],
    menu: dict[str, MenuItem],
    staff: dict[str, Staff],
) -> OperationsReport:
    if not orders:
        return OperationsReport()

    total_revenue = sum(p.amount for o in orders for p in o.payments)
    avg_ticket = total_revenue / len(orders) if orders else 0.0

    hour_counts: dict[int, int] = defaultdict(int)
    hour_revenue: dict[int, float] = defaultdict(float)
    dow_dates: dict[int, set[date]] = defaultdict(set)
    dow_counts: dict[int, int] = defaultdict(int)
    dow_revenue: dict[int, float] = defaultdict(float)
    dp_counts: dict[str, int] = defaultdict(int)
    dp_revenue: dict[str, float] = defaultdict(float)
    ch_counts: dict[str, int] = defaultdict(int)
    ch_revenue: dict[str, float] = defaultdict(float)
    pay_counts: dict[str, int] = defaultdict(int)
    pay_revenue: dict[str, float] = defaultdict(float)
    item_vol: dict[str, int] = defaultdict(int)
    item_rev: dict[str, float] = defaultdict(float)

    # weekday afternoon tracking
    wday_afternoon_counts: dict[int, int] = defaultdict(int)
    wday_afternoon_dates: dict[int, set[date]] = defaultdict(set)

    for o in orders:
        h = o.datetime.hour
        d = o.datetime.date()
        dow = o.datetime.weekday()
        rev = sum(p.amount for p in o.payments)
        method = o.payments[0].method if o.payments else "unknown"

        hour_counts[h] += 1
        hour_revenue[h] += rev
        dow_dates[dow].add(d)
        dow_counts[dow] += 1
        dow_revenue[dow] += rev
        ch_counts[o.channel] += 1
        ch_revenue[o.channel] += rev
        pay_counts[method] += 1
        pay_revenue[method] += rev

        for dp_name, dp_start, dp_end in DAYPARTS:
            if dp_start <= h < dp_end:
                dp_counts[dp_name] += 1
                dp_revenue[dp_name] += rev
                break

        for li in o.line_items:
            if not li.is_void and not li.is_comp:
                item_vol[li.item_sku] += li.qty
                item_rev[li.item_sku] += li.line_amount

    hours = [HourStats(hour=h, order_count=hour_counts.get(h, 0),
                       revenue=hour_revenue.get(h, 0.0)) for h in range(24)]

    days = []
    for dow in range(7):
        occ = len(dow_dates.get(dow, set()))
        cnt = dow_counts.get(dow, 0)
        rev = dow_revenue.get(dow, 0.0)
        days.append(DayOfWeekStats(
            day=dow, day_name=DAY_NAMES[dow],
            order_count=cnt, revenue=rev,
            num_occurrences=max(occ, 1),
            avg_orders=cnt / max(occ, 1),
            avg_revenue=rev / max(occ, 1),
        ))

    dayparts = []
    for dp_name, dp_start, dp_end in DAYPARTS:
        cnt = dp_counts.get(dp_name, 0)
        rev = dp_revenue.get(dp_name, 0.0)
        dayparts.append(DaypartStats(
            name=dp_name, start_hour=dp_start, end_hour=dp_end,
            order_count=cnt, revenue=rev,
            avg_ticket=rev / cnt if cnt else 0.0,
        ))

    channels = []
    for ch in sorted(ch_counts.keys()):
        cnt = ch_counts[ch]
        rev = ch_revenue[ch]
        channels.append(ChannelStats(
            channel=ch, order_count=cnt, revenue=rev,
            avg_ticket=rev / cnt if cnt else 0.0,
        ))

    total_pay = sum(pay_counts.values())
    payment_shares = []
    for m in sorted(pay_counts.keys()):
        payment_shares.append(PaymentShareStats(
            method=m, order_count=pay_counts[m],
            share_pct=pay_counts[m] / total_pay if total_pay else 0.0,
            revenue=pay_revenue[m],
        ))

    items: dict[str, ItemRank] = {}
    for sku in set(list(item_vol.keys()) + list(menu.keys())):
        mi = menu.get(sku)
        items[sku] = ItemRank(
            sku=sku,
            name=mi.name if mi else sku,
            category=mi.category if mi else "",
            volume=item_vol.get(sku, 0),
            revenue=item_rev.get(sku, 0.0),
            margin=mi.margin if mi else None,
            margin_contribution=(
                item_rev.get(sku, 0.0) * mi.margin if mi and mi.margin else 0.0
            ),
        )

    by_volume = sorted(items.values(), key=lambda x: x.volume, reverse=True)
    by_margin = sorted(
        [i for i in items.values() if i.margin is not None],
        key=lambda x: x.margin,
    )

    active_hours = [h for h in hours if h.order_count > 0]
    busiest_h = max(active_hours, key=lambda h: h.order_count)
    deadest_h = min(active_hours, key=lambda h: h.order_count)

    busiest_dow = max(days, key=lambda d: d.avg_orders)
    deadest_dow = min(days, key=lambda d: d.avg_orders)

    active_dp = [d for d in dayparts if d.order_count > 0]
    busiest_dp = max(active_dp, key=lambda d: d.order_count)
    deadest_dp = min(active_dp, key=lambda d: d.order_count)

    return OperationsReport(
        avg_ticket=avg_ticket,
        hours=hours,
        days_of_week=days,
        dayparts=dayparts,
        channels=channels,
        payment_shares=payment_shares,
        items_by_volume=by_volume,
        items_by_margin=by_margin,
        busiest_hour=busiest_h.hour,
        deadest_hour=deadest_h.hour,
        busiest_day=busiest_dow.day_name,
        deadest_day=deadest_dow.day_name,
        busiest_daypart=busiest_dp.name,
        deadest_daypart=deadest_dp.name,
    )
