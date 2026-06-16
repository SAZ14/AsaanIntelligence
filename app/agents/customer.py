"""Customer agent — a thin live wrapper over the deterministic retention core.

This module does NOT recompute retention analytics. It imports
``analyze_retention`` (``app/analysis/retention.py``) for the per-customer
profiles + lapse detection, and reuses ``compute_headlines`` /
``_observed_monthly_spend`` (``app/report/render.py``) for the
recovery-adjusted win-back maths. On top of that it adds purely
customer-facing concerns: segmentation, behavioural descriptors, and the
recovery-adjusted value per lapsed customer.

WIN-BACK VALUE — IMPORTANT
    The only win-back figure this agent reports is the *recovery-adjusted*
    one from ``compute_headlines`` (observed monthly spend × recovery rate,
    default 30%). The naive ``CustomerProfile.winback_value`` field in
    retention.py (days_since_last / cadence × avg_ticket) is intentionally
    NEVER read here — it overstates the opportunity. Do not surface it.

PRIVACY
    Customers are recognised by their tokenised payment reference
    (``customer_ref``) only. There are no names anywhere in this agent's
    output — descriptors and alerts describe *behaviour*, not identity.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from app.analysis.retention import (
    MIN_VISITS_FOR_CADENCE,
    CustomerProfile,
    RetentionReport,
    analyze_retention,
)
from app.models.canonical import MenuItem, Order, Staff
from app.report.render import (
    DEFAULT_RECOVERY_RATE,
    HeadlineNumbers,
    _observed_monthly_spend,
    compute_headlines,
)

# Re-export the integrity/operations report types only to build the empty
# placeholders compute_headlines needs (its win-back maths reads neither).
from app.analysis.integrity import IntegrityReport, VenueBaseline
from app.analysis.retention import OperationsReport

# ── Segmentation thresholds ──
VIP_SPEND_PERCENTILE = 0.80   # top 20% of identified customers by total spend
NEW_MAX_VISITS = 2            # "new" customers have at most this many visits
NEW_WINDOW_DAYS = 14          # ...and first showed up within this window of period end


# ── Output types ──

@dataclass
class CustomerDescriptor:
    """Behavioural fingerprint of one customer — no personal identity."""
    customer_ref: str
    visit_count: int
    median_cadence_days: float | None
    days_since_last: int
    last_visit: date | None
    avg_ticket: float
    total_spend: float
    top_items: list[str] = field(default_factory=list)
    is_vip: bool = False
    # Recovery-adjusted monthly value (observed spend rate × recovery rate).
    recovery_adjusted_value: float = 0.0


@dataclass
class CustomerSegments:
    # Frequency tiers (a partition of all identified customers):
    regulars: list[CustomerDescriptor] = field(default_factory=list)
    new: list[CustomerDescriptor] = field(default_factory=list)
    occasional: list[CustomerDescriptor] = field(default_factory=list)
    # Value tier (cross-cutting — VIPs may also be regulars/occasional):
    vips: list[CustomerDescriptor] = field(default_factory=list)


@dataclass
class CustomerAgentReport:
    venue_name: str = ""
    recovery_rate: float = DEFAULT_RECOVERY_RATE
    unique_customers: int = 0
    coverage_order_pct: float = 0.0
    coverage_revenue_pct: float = 0.0
    segments: CustomerSegments = field(default_factory=CustomerSegments)
    lapsed: list[CustomerDescriptor] = field(default_factory=list)
    lapsed_vips: list[CustomerDescriptor] = field(default_factory=list)
    lapsed_regular_count: int = 0
    # The only win-back numbers we report — both recovery-adjusted:
    total_recoverable_monthly: float = 0.0            # Tier A (lapsed regulars)
    total_recoverable_with_at_risk: float = 0.0       # Tier A + B


# ── Helpers ──

def _period_days(orders: list[Order]) -> int:
    if not orders:
        return 0
    dates = [o.datetime.date() for o in orders]
    return (max(dates) - min(dates)).days + 1


def recovery_headlines(
    retention: RetentionReport,
    orders: list[Order] | None = None,
    recovery_rate: float = DEFAULT_RECOVERY_RATE,
) -> HeadlineNumbers:
    """Reuse ``compute_headlines`` for win-back only.

    The Tier A/B win-back fields in HeadlineNumbers depend solely on the
    retention report, so we hand compute_headlines empty integrity/operations
    placeholders (period_days keeps the revenue ratio sensible) rather than
    coupling the Customer agent to the Integrity agent.
    """
    period_days = _period_days(orders) if orders else 0
    integrity = IntegrityReport(venue_baseline=VenueBaseline(period_days=period_days))
    operations = OperationsReport()
    return compute_headlines(integrity, retention, operations, recovery_rate=recovery_rate)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(pct * (len(s) - 1) + 0.5))
    return s[idx]


def _top_items_by_customer(orders: list[Order], top_n: int = 3) -> dict[str, list[str]]:
    """Most-ordered item names per customer_ref (voids/comps excluded)."""
    counts: dict[str, Counter] = defaultdict(Counter)
    for o in orders:
        if not o.customer_ref:
            continue
        for li in o.line_items:
            if li.is_void or li.is_comp:
                continue
            counts[o.customer_ref][li.item_name] += li.qty
    return {ref: [name for name, _ in c.most_common(top_n)] for ref, c in counts.items()}


# ── Main agent ──

def run_customer_agent(
    retention: RetentionReport,
    orders: list[Order],
    menu: dict[str, MenuItem] | None = None,
    staff: dict[str, Staff] | None = None,
    *,
    venue_name: str = "Sugar Rush",
    recovery_rate: float = DEFAULT_RECOVERY_RATE,
    headlines: HeadlineNumbers | None = None,
) -> CustomerAgentReport:
    """Build the live Customer agent report from an existing RetentionReport.

    ``retention`` is the output of ``analyze_retention``; pass ``headlines`` to
    reuse already-computed win-back numbers, otherwise they are derived here
    via ``recovery_headlines`` (which calls ``compute_headlines``).
    """
    if headlines is None:
        headlines = recovery_headlines(retention, orders, recovery_rate)

    top_items = _top_items_by_customer(orders)
    rate = headlines.recovery_rate

    def _descriptor(p: CustomerProfile, vip: bool = False) -> CustomerDescriptor:
        return CustomerDescriptor(
            customer_ref=p.customer_ref,
            visit_count=p.visit_count,
            median_cadence_days=p.median_cadence_days,
            days_since_last=p.days_since_last,
            last_visit=p.last_visit,
            avg_ticket=p.avg_ticket,
            total_spend=p.total_spend,
            top_items=top_items.get(p.customer_ref, []),
            is_vip=vip,
            recovery_adjusted_value=_observed_monthly_spend(p) * rate,
        )

    profiles = retention.customers
    period_end = max((p.last_visit for p in profiles if p.last_visit), default=None)

    # VIP = top-spend tier among repeat customers (cross-cutting value tier).
    spend_cut = _percentile([p.total_spend for p in profiles], VIP_SPEND_PERCENTILE)
    vip_refs = {
        p.customer_ref for p in profiles
        if p.visit_count >= 2 and p.total_spend >= spend_cut and spend_cut > 0
    }

    def _is_regular(p: CustomerProfile) -> bool:
        return (
            p.median_cadence_days is not None
            and p.median_cadence_days <= retention.cadence_threshold_days
            and p.visit_count >= MIN_VISITS_FOR_CADENCE
        )

    def _is_new(p: CustomerProfile) -> bool:
        if p.visit_count > NEW_MAX_VISITS or p.first_visit is None or period_end is None:
            return False
        return (period_end - p.first_visit).days <= NEW_WINDOW_DAYS

    segments = CustomerSegments()
    for p in profiles:
        d = _descriptor(p, vip=p.customer_ref in vip_refs)
        if d.is_vip:
            segments.vips.append(d)
        # Frequency partition: regular > new > occasional.
        if _is_regular(p):
            segments.regulars.append(d)
        elif _is_new(p):
            segments.new.append(d)
        else:
            segments.occasional.append(d)

    # Lapsed list = Tier A winnable (lapsed regulars within the recovery window),
    # so it stays consistent with total_recoverable_monthly.
    lapsed = [_descriptor(p, vip=p.customer_ref in vip_refs) for p in headlines.tier_a_winnable]
    lapsed.sort(key=lambda d: d.recovery_adjusted_value, reverse=True)
    lapsed_vips = [d for d in lapsed if d.is_vip]

    return CustomerAgentReport(
        venue_name=venue_name,
        recovery_rate=rate,
        unique_customers=retention.unique_customers,
        coverage_order_pct=retention.coverage_order_pct,
        coverage_revenue_pct=retention.coverage_revenue_pct,
        segments=segments,
        lapsed=lapsed,
        lapsed_vips=lapsed_vips,
        lapsed_regular_count=retention.lapsed_regular_count,
        total_recoverable_monthly=headlines.monthly_winback_tier_a,
        total_recoverable_with_at_risk=headlines.monthly_winback_total,
    )


def run_from_dataset(
    orders: list[Order],
    menu: dict[str, MenuItem],
    staff: dict[str, Staff],
    *,
    venue_name: str = "Sugar Rush",
    recovery_rate: float = DEFAULT_RECOVERY_RATE,
) -> CustomerAgentReport:
    """Convenience: run retention analysis then the Customer agent."""
    retention = analyze_retention(orders, menu, staff)
    return run_customer_agent(
        retention, orders, menu, staff,
        venue_name=venue_name, recovery_rate=recovery_rate,
    )


# ── WhatsApp alert formatting ──

def format_lapsed_vip_alert(d: CustomerDescriptor, venue_name: str = "Sugar Rush") -> str:
    """Owner-facing alert describing a lapsed VIP by behaviour, never identity."""
    if not d.median_cadence_days:
        cadence = "on an irregular cadence"
    elif round(d.median_cadence_days) <= 1:
        cadence = "almost daily"
    else:
        cadence = f"about every {d.median_cadence_days:.0f} days"
    items = ", ".join(d.top_items) if d.top_items else "a mix of items"
    return (
        f"[{venue_name}] Lapsed regular alert\n"
        f"Customer {d.customer_ref} (recognised by payment token only — no name on file).\n"
        f"Behaviour: visited {d.visit_count}x {cadence}, "
        f"avg ticket PKR {d.avg_ticket:,.0f}; usual order: {items}.\n"
        f"Last seen {d.days_since_last} days ago — well past their usual cadence.\n"
        f"Recovery-adjusted value if won back: ~PKR {d.recovery_adjusted_value:,.0f}/month.\n"
        f"Suggested action: queue a win-back offer."
    )
