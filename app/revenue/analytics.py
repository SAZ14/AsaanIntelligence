"""The Revenue analytics engine: product performance, dead-window detection,
brand-safe campaign recommendations and the owner digest.

Reuses ``app.analysis.retention.analyze_operations`` for item ranking and the
shared daypart definitions, then adds a day-of-week × daypart grid (so we can
say "Tuesday 3–6 PM" specifically) and the campaign maths.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from app.analysis.retention import DAY_NAMES, DAYPARTS, analyze_operations
from app.models.canonical import MenuItem, Order, Staff
from app.revenue.config import RevenueConfig
from app.revenue.pricing import PricingRec, compute_pricing_recommendations
from app.revenue.segments import Segment, build_segments


# ── dataclasses ──

@dataclass
class ProductStat:
    sku: str
    name: str
    category: str
    units: int
    revenue: float
    margin: float | None = None
    margin_contribution: float = 0.0


@dataclass
class ProductPerformance:
    period_label: str
    total_revenue: float
    order_count: int
    avg_ticket: float
    top_sellers: list[ProductStat] = field(default_factory=list)
    top_margin: list[ProductStat] = field(default_factory=list)
    dogs: list[ProductStat] = field(default_factory=list)   # low-margin laggards


@dataclass
class DeadWindow:
    day_name: str
    daypart: str
    start_hour: int
    end_hour: int
    avg_orders: float
    avg_revenue: float
    gap_vs_peak: float        # how many orders/occurrence below the peak window


@dataclass
class CampaignRec:
    window_desc: str
    campaign_key: str
    campaign_name: str
    target_segment: str
    audience_size: int
    expected_redemptions: int
    est_added_revenue: float
    message: str


# ── product performance ──

def product_performance(
    orders: list[Order],
    menu: dict[str, MenuItem],
    staff: dict[str, Staff],
    period_label: str = "",
    top_n: int = 5,
) -> ProductPerformance:
    ops = analyze_operations(orders, menu, staff)

    def to_stat(rank) -> ProductStat:
        return ProductStat(
            sku=rank.sku, name=rank.name, category=rank.category,
            units=rank.volume, revenue=round(rank.revenue, 0),
            margin=rank.margin, margin_contribution=round(rank.margin_contribution, 0),
        )

    sold = [r for r in ops.items_by_volume if r.volume > 0]
    top_sellers = [to_stat(r) for r in sold[:top_n]]
    by_margin_contrib = sorted(sold, key=lambda r: r.margin_contribution, reverse=True)
    top_margin = [to_stat(r) for r in by_margin_contrib[:top_n]]
    # "dogs" = items that sell but earn the worst margin.
    dogs = [to_stat(r) for r in ops.items_by_margin if r.volume > 0][:3]

    total_rev = sum(p.amount for o in orders for p in o.payments)
    return ProductPerformance(
        period_label=period_label,
        total_revenue=round(total_rev, 0),
        order_count=len(orders),
        avg_ticket=round(ops.avg_ticket, 0),
        top_sellers=top_sellers,
        top_margin=top_margin,
        dogs=dogs,
    )


# ── dead-window detection (day-of-week × daypart) ──

def detect_dead_windows(
    orders: list[Order], top_n: int = 3, min_occurrences: int = 2
) -> list[DeadWindow]:
    if not orders:
        return []

    counts: dict[tuple[int, str], int] = defaultdict(int)
    revenue: dict[tuple[int, str], float] = defaultdict(float)
    occ_dates: dict[tuple[int, str], set[date]] = defaultdict(set)
    seen_days: dict[int, set[date]] = defaultdict(set)

    dp_bounds = {name: (s, e) for name, s, e in DAYPARTS}

    for o in orders:
        dow = o.datetime.weekday()
        h = o.datetime.hour
        d = o.datetime.date()
        seen_days[dow].add(d)
        rev = sum(p.amount for p in o.payments)
        for name, s, e in DAYPARTS:
            if s <= h < e:
                key = (dow, name)
                counts[key] += 1
                revenue[key] += rev
                occ_dates[key].add(d)
                break

    cells: list[DeadWindow] = []
    for (dow, daypart), cnt in counts.items():
        # Occurrences = how many of that weekday actually appear in the data
        # (a window only "exists" if the venue was open then).
        occ = max(len(occ_dates[(dow, daypart)]), 1)
        weekday_occ = max(len(seen_days[dow]), 1)
        avg_orders = cnt / weekday_occ
        s, e = dp_bounds[daypart]
        cells.append(DeadWindow(
            day_name=DAY_NAMES[dow], daypart=daypart, start_hour=s, end_hour=e,
            avg_orders=round(avg_orders, 1),
            avg_revenue=round(revenue[(dow, daypart)] / weekday_occ, 0),
            gap_vs_peak=0.0,
        ))
        if occ < min_occurrences:
            cells.pop()  # too few samples to trust

    if not cells:
        return []

    peak = max(c.avg_orders for c in cells)
    for c in cells:
        c.gap_vs_peak = round(peak - c.avg_orders, 1)

    cells.sort(key=lambda c: c.avg_orders)
    return cells[:top_n]


# ── campaign recommendations ──

def recommend_campaigns(
    dead_windows: list[DeadWindow],
    segments: dict[str, Segment],
    config: RevenueConfig | None = None,
    overall_avg_ticket: float = 0.0,
) -> list[CampaignRec]:
    config = config or RevenueConfig()
    recs: list[CampaignRec] = []

    for win in dead_windows:
        choices = config.campaigns_for_daypart(win.daypart)
        # Prefer a campaign whose target segment actually has people in it.
        campaign = next(
            (c for c in choices if segments.get(c.target_segment)
             and segments[c.target_segment].size > 0),
            choices[0],
        )
        seg = segments.get(campaign.target_segment)
        if seg is None or seg.size == 0:
            continue

        audience = seg.size
        redemptions = seg.expected_redemptions(audience)
        ticket = seg.avg_ticket or overall_avg_ticket
        gross = redemptions * ticket
        cost = redemptions * campaign.est_cost_per_redemption
        added = round(gross - cost, 0)

        window_desc = f"{win.day_name} {win.daypart} ({win.start_hour:02d}:00–{win.end_hour:02d}:00)"
        message = (
            f"{window_desc} is underutilized "
            f"(~{win.avg_orders:.0f} orders vs {win.avg_orders + win.gap_vs_peak:.0f} at peak). "
            f"Send “{campaign.name}” to {audience} {seg.label}. "
            f"Expected ~{redemptions} redemptions, ≈ PKR {added:,.0f} added revenue."
        )
        recs.append(CampaignRec(
            window_desc=window_desc, campaign_key=campaign.key,
            campaign_name=campaign.name, target_segment=campaign.target_segment,
            audience_size=audience, expected_redemptions=redemptions,
            est_added_revenue=added, message=message,
        ))

    recs.sort(key=lambda r: r.est_added_revenue, reverse=True)
    return recs


# ── full digest ──

@dataclass
class RevenueDigest:
    period_label: str
    performance: ProductPerformance
    pricing: list[PricingRec]
    dead_windows: list[DeadWindow]
    campaigns: list[CampaignRec]
    identified_caveat: str = ""


def build_digest(
    orders: list[Order],
    menu: dict[str, MenuItem],
    staff: dict[str, Staff],
    period_label: str,
    config: RevenueConfig | None = None,
    period_days: int = 7,
) -> RevenueDigest:
    config = config or RevenueConfig()
    perf = product_performance(orders, menu, staff, period_label=period_label)
    pricing = compute_pricing_recommendations(orders, menu, config, period_days=period_days)
    windows = detect_dead_windows(orders)
    segments = build_segments(orders, menu, staff, config)
    campaigns = recommend_campaigns(windows, segments, config, perf.avg_ticket)

    identified = sum(1 for o in orders if o.customer_ref)
    caveat = ""
    if orders and identified / len(orders) < 0.95:
        caveat = ("Targeting covers carded/wallet customers only; anonymous cash "
                  "visits aren't reachable.")

    return RevenueDigest(
        period_label=period_label, performance=perf, pricing=pricing,
        dead_windows=windows, campaigns=campaigns, identified_caveat=caveat,
    )
