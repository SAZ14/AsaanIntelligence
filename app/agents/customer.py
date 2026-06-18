"""Customer Agent — virtual loyalty program + lapse alerts + personalized incentives.

Tagline: "You never quietly lose a high-value guest again."

Built on the retention engine. QR-linked profiles (customer_ref) power outbound
incentive messages without a separate loyalty POS integration.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.analysis.retention import CustomerProfile, analyze_retention
from app.models.canonical import LoyaltyCustomer, LoyaltyRules, MenuItem, Order, Staff

TAGLINE = "You never quietly lose a high-value guest again."

# Segmentation thresholds
CORPORATE_MAX_VISITS = 6
CORPORATE_TICKET_PERCENTILE = 0.75
BANQUET_TICKET_MULTIPLIER = 2.0
HIGH_VALUE_SPEND_PERCENTILE = 0.80
STREAK_WINDOW_DAYS = 7
STREAK_MIN_VISITS = 3

# Loyalty tiers (visit-based, with spend override)
TIER_SILVER_VISITS = 10
TIER_GOLD_VISITS = 20
TIER_PLATINUM_VISITS = 35

# Incentive defaults
MILESTONE_VISIT_INTERVAL = 5
MILESTONE_DISCOUNT_PCT = 10
WINBACK_LAPSED_DISCOUNT_PCT = 15
WINBACK_LAPSING_DISCOUNT_PCT = 10
CORPORATE_DISCOUNT_PCT = 5
STREAK_REWARD = "complimentary dessert on your next visit"


# ── Data classes ──


@dataclass
class OrderStats:
    order_count: int = 0
    max_ticket: float = 0.0
    favorite_items: list[str] = field(default_factory=list)
    preferred_channel: str = ""
    visit_dates: list[date] = field(default_factory=list)
    recent_visit_count: int = 0  # visits in STREAK_WINDOW_DAYS before period end


@dataclass
class EnrichedCustomer:
    profile: CustomerProfile
    segment: str = "occasional"
    loyalty_tier: str = "none"
    monthly_value: float = 0.0
    order_stats: OrderStats = field(default_factory=OrderStats)
    qr_linked: bool = False
    display_name: str = ""
    contact_channel: str = ""
    contact_phone: str = ""
    opted_in: bool = False


@dataclass
class LapseAlert:
    customer_ref: str
    display_name: str
    segment: str
    loyalty_tier: str
    days_since_last: int
    winback_value: float
    monthly_value: float
    urgency: str  # critical | high | medium
    message: str = ""


@dataclass
class CustomerIncentive:
    customer_ref: str
    display_name: str
    incentive_type: str
    discount_pct: float | None
    reward_text: str
    message: str
    channel: str
    phone: str
    priority: int  # lower = send first
    trigger_reason: str


@dataclass
class LoyaltyProgramSummary:
    total_members: int = 0
    qr_linked_members: int = 0
    tier_bronze: int = 0
    tier_silver: int = 0
    tier_gold: int = 0
    tier_platinum: int = 0
    active_regulars: int = 0
    at_risk_count: int = 0
    lapsed_count: int = 0


@dataclass
class CustomerAgentReport:
    venue_name: str = ""
    tagline: str = TAGLINE
    period_days: int = 0
    loyalty: LoyaltyProgramSummary = field(default_factory=LoyaltyProgramSummary)
    customers: list[EnrichedCustomer] = field(default_factory=list)
    high_value: list[EnrichedCustomer] = field(default_factory=list)
    corporate_accounts: list[EnrichedCustomer] = field(default_factory=list)
    banquet_accounts: list[EnrichedCustomer] = field(default_factory=list)
    lapse_alerts: list[LapseAlert] = field(default_factory=list)
    incentives: list[CustomerIncentive] = field(default_factory=list)
    total_winback_at_risk: float = 0.0
    messages_ready: int = 0


# ── Order-level enrichment ──


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(int(len(s) * pct), len(s) - 1)
    return s[idx]


def _build_order_stats(
    orders: list[Order],
    period_end: date,
    rules: LoyaltyRules | None = None,
) -> dict[str, OrderStats]:
    rules = rules or LoyaltyRules()
    by_customer: dict[str, OrderStats] = defaultdict(OrderStats)
    item_counts: dict[str, Counter[str]] = defaultdict(Counter)
    channel_counts: dict[str, Counter[str]] = defaultdict(Counter)
    order_tickets: dict[str, dict[str, float]] = defaultdict(dict)

    streak_start = period_end - timedelta(days=rules.streak_window_days)

    for o in orders:
        cref = o.customer_ref
        if not cref:
            continue
        rev = sum(p.amount for p in o.payments)
        d = o.datetime.date()
        stats = by_customer[cref]
        stats.visit_dates.append(d)
        if d >= streak_start:
            stats.recent_visit_count += 1

        if o.order_id not in order_tickets[cref]:
            order_tickets[cref][o.order_id] = rev
            stats.order_count += 1
            stats.max_ticket = max(stats.max_ticket, rev)

        channel_counts[cref][o.channel] += 1
        for li in o.line_items:
            if not li.is_void and not li.is_comp:
                item_counts[cref][li.item_name] += li.qty

    for cref, stats in by_customer.items():
        stats.visit_dates = sorted(set(stats.visit_dates))
        if item_counts[cref]:
            stats.favorite_items = [n for n, _ in item_counts[cref].most_common(3)]
        if channel_counts[cref]:
            stats.preferred_channel = channel_counts[cref].most_common(1)[0][0]

    return dict(by_customer)


def _monthly_value(profile: CustomerProfile, period_days: int) -> float:
    if profile.first_visit is None or profile.last_visit is None:
        return 0.0
    span = max((profile.last_visit - profile.first_visit).days, 1)
    return profile.total_spend / span * 30


def _loyalty_tier(profile: CustomerProfile, spend_rank_pct: float) -> str:
    vc = profile.visit_count
    if vc >= TIER_PLATINUM_VISITS or spend_rank_pct >= 0.95:
        return "platinum"
    if vc >= TIER_GOLD_VISITS or spend_rank_pct >= 0.90:
        return "gold"
    if vc >= TIER_SILVER_VISITS or spend_rank_pct >= 0.75:
        return "silver"
    if vc >= 3:
        return "bronze"
    return "none"


def _segment_customer(
    profile: CustomerProfile,
    stats: OrderStats,
    ticket_p75: float,
    ticket_p90: float,
    spend_threshold: float,
    is_regular: bool,
) -> str:
    if stats.max_ticket >= ticket_p90 * BANQUET_TICKET_MULTIPLIER:
        return "banquet"
    if (
        profile.visit_count <= CORPORATE_MAX_VISITS
        and profile.avg_ticket >= ticket_p75
        and profile.avg_ticket > 0
    ):
        return "corporate"
    if profile.total_spend >= spend_threshold or profile.is_lapsed_regular:
        return "high_value"
    if is_regular:
        return "regular"
    if profile.visit_count <= 2:
        return "occasional"
    return "returning"


def _guest_name(cref: str, registry: dict[str, LoyaltyCustomer]) -> str:
    if cref in registry and registry[cref].display_name:
        return registry[cref].display_name
    return f"Guest {cref[-4:].upper()}"


def _urgency(profile: CustomerProfile, segment: str) -> str:
    if profile.is_lapsed_regular and segment in ("high_value", "corporate", "banquet", "regular"):
        return "critical"
    if profile.is_lapsed_regular:
        return "high"
    if profile.is_lapsing and segment in ("high_value", "corporate", "regular"):
        return "high"
    if profile.is_lapsing:
        return "medium"
    return "medium"


# ── Incentive engine ──


def _build_incentives(
    enriched: EnrichedCustomer,
    venue_name: str,
    rules: LoyaltyRules | None = None,
) -> list[CustomerIncentive]:
    rules = rules or LoyaltyRules()
    p = enriched.profile
    stats = enriched.order_stats
    name = enriched.display_name
    cref = p.customer_ref
    channel = enriched.contact_channel or "sms"
    phone = enriched.contact_phone
    out: list[CustomerIncentive] = []

    if enriched.qr_linked and not enriched.opted_in:
        return out

    fav = stats.favorite_items[0] if stats.favorite_items else "your usual"

    if p.is_lapsed_regular:
        pct = rules.winback_lapsed_discount_pct
        out.append(CustomerIncentive(
            customer_ref=cref,
            display_name=name,
            incentive_type="winback_lapsed",
            discount_pct=pct,
            reward_text=f"{pct:.0f}% off your next visit",
            message=(
                f"Hi {name}, we haven't seen you at {venue_name} in {p.days_since_last} days — "
                f"and we miss you. Enjoy {pct:.0f}% off your next order. "
                f"Your table (and your {fav}) are waiting."
            ),
            channel=channel,
            phone=phone,
            priority=1,
            trigger_reason=f"lapsed regular, {p.days_since_last}d since last visit",
        ))
    elif p.is_lapsing:
        pct = rules.winback_lapsing_discount_pct
        out.append(CustomerIncentive(
            customer_ref=cref,
            display_name=name,
            incentive_type="winback_lapsing",
            discount_pct=pct,
            reward_text=f"{pct:.0f}% off this week",
            message=(
                f"Hi {name}, it's been a little while since your last visit to {venue_name}. "
                f"Drop by this week for {pct:.0f}% off — "
                f"you usually come every {p.median_cadence_days:.0f} days and we'd love to see you."
            ),
            channel=channel,
            phone=phone,
            priority=2,
            trigger_reason=f"lapsing ({p.lapse_multiplier:.1f}x normal cadence)",
        ))

    interval = rules.milestone_visit_interval
    milestone_pct = rules.milestone_discount_pct
    if (
        p.visit_count >= interval
        and p.visit_count % interval == 0
        and not p.is_lapsing
        and not p.is_lapsed_regular
    ):
        out.append(CustomerIncentive(
            customer_ref=cref,
            display_name=name,
            incentive_type="visit_milestone",
            discount_pct=milestone_pct,
            reward_text=f"{milestone_pct:.0f}% off — {p.visit_count} visits!",
            message=(
                f"Hi {name}, you've visited {venue_name} {p.visit_count} times — "
                f"thank you for being part of our regulars. "
                f"Here's {milestone_pct:.0f}% off your next order on us."
            ),
            channel=channel,
            phone=phone,
            priority=3,
            trigger_reason=f"{p.visit_count}th visit milestone",
        ))

    if stats.recent_visit_count >= rules.streak_min_visits and not p.is_lapsing:
        out.append(CustomerIncentive(
            customer_ref=cref,
            display_name=name,
            incentive_type="visit_streak",
            discount_pct=None,
            reward_text=rules.streak_reward,
            message=(
                f"Hi {name}, {stats.recent_visit_count} visits in the last {rules.streak_window_days} days — "
                f"you're on a roll at {venue_name}! "
                f"Enjoy a {rules.streak_reward} next time you scan in."
            ),
            channel=channel,
            phone=phone,
            priority=4,
            trigger_reason=f"{stats.recent_visit_count} visits in {rules.streak_window_days}d",
        ))

    corp_pct = rules.corporate_discount_pct
    if enriched.segment == "corporate" and p.visit_count >= 1 and not p.is_lapsing:
        out.append(CustomerIncentive(
            customer_ref=cref,
            display_name=name,
            incentive_type="corporate_thanks",
            discount_pct=corp_pct,
            reward_text=f"{corp_pct:.0f}% off your next booking",
            message=(
                f"Hi {name}, thank you for choosing {venue_name} for your team. "
                f"We'd love to host you again — {corp_pct:.0f}% off your next booking."
            ),
            channel=channel,
            phone=phone,
            priority=5,
            trigger_reason="corporate account",
        ))

    if enriched.loyalty_tier in ("gold", "platinum") and p.visit_count >= TIER_GOLD_VISITS:
        if not any(i.incentive_type.startswith("winback") for i in out):
            out.append(CustomerIncentive(
                customer_ref=cref,
                display_name=name,
                incentive_type="tier_recognition",
                discount_pct=5.0,
                reward_text="VIP thank-you — 5% off",
                message=(
                    f"Hi {name}, as one of our {enriched.loyalty_tier} guests at {venue_name}, "
                    f"thank you for your loyalty. Scan in for 5% off today."
                ),
                channel=channel,
                phone=phone,
                priority=6,
                trigger_reason=f"{enriched.loyalty_tier} tier recognition",
            ))

    return out


def _build_lapse_alert(enriched: EnrichedCustomer, venue_name: str) -> LapseAlert | None:
    p = enriched.profile
    if not (p.is_lapsing or p.is_lapsed_regular):
        return None
    urgency = _urgency(p, enriched.segment)
    name = enriched.display_name
    if p.is_lapsed_regular:
        msg = (
            f"{name} ({enriched.segment}) has stopped visiting — last seen {p.days_since_last}d ago. "
            f"Worth ~PKR {enriched.monthly_value:,.0f}/mo to win back."
        )
    else:
        msg = (
            f"{name} ({enriched.segment}) is overdue — {p.lapse_multiplier:.1f}x their usual cadence. "
            f"Act before they quietly churn."
        )
    return LapseAlert(
        customer_ref=p.customer_ref,
        display_name=name,
        segment=enriched.segment,
        loyalty_tier=enriched.loyalty_tier,
        days_since_last=p.days_since_last,
        winback_value=p.winback_value,
        monthly_value=enriched.monthly_value,
        urgency=urgency,
        message=msg,
    )


# ── Main agent ──


def run_customer_agent(
    orders: list[Order],
    menu: dict[str, MenuItem],
    staff: dict[str, Staff],
    registry: dict[str, LoyaltyCustomer] | None = None,
    venue_name: str = "your venue",
    rules: LoyaltyRules | None = None,
) -> CustomerAgentReport:
    """Run the Customer Agent: retention + segmentation + incentives + lapse alerts."""
    registry = registry or {}
    rules = rules or LoyaltyRules()
    retention = analyze_retention(orders, menu, staff)

    if not orders:
        return CustomerAgentReport(venue_name=venue_name)

    period_end = max(o.datetime.date() for o in orders)
    period_start = min(o.datetime.date() for o in orders)
    period_days = max(1, (period_end - period_start).days + 1)

    order_stats = _build_order_stats(orders, period_end, rules)

    all_tickets = [
        sum(p.amount for p in o.payments)
        for o in orders if o.customer_ref
    ]
    ticket_p75 = _percentile(all_tickets, CORPORATE_TICKET_PERCENTILE)
    ticket_p90 = _percentile(all_tickets, 0.90)

    spends = [p.total_spend for p in retention.customers]
    spend_threshold = _percentile(spends, HIGH_VALUE_SPEND_PERCENTILE) if spends else 0.0
    max_spend = max(spends) if spends else 1.0

    regular_refs = {
        p.customer_ref for p in retention.customers
        if p.median_cadence_days is not None
        and p.median_cadence_days <= retention.cadence_threshold_days
        and p.visit_count >= 3
    }

    enriched_list: list[EnrichedCustomer] = []
    for profile in retention.customers:
        stats = order_stats.get(profile.customer_ref, OrderStats())
        spend_rank = profile.total_spend / max_spend if max_spend else 0.0
        reg = registry.get(profile.customer_ref)

        ec = EnrichedCustomer(
            profile=profile,
            segment=_segment_customer(
                profile, stats, ticket_p75, ticket_p90, spend_threshold,
                profile.customer_ref in regular_refs,
            ),
            loyalty_tier=_loyalty_tier(profile, spend_rank),
            monthly_value=_monthly_value(profile, period_days),
            order_stats=stats,
            qr_linked=reg is not None,
            display_name=reg.display_name if reg else _guest_name(profile.customer_ref, registry),
            contact_channel=reg.channel if reg else "",
            contact_phone=reg.phone if reg else "",
            opted_in=reg.opted_in if reg else False,
        )
        enriched_list.append(ec)

    high_value = [e for e in enriched_list if e.segment in ("high_value", "regular")
                  and e.profile.visit_count >= 3]
    high_value.sort(key=lambda e: e.profile.total_spend, reverse=True)

    corporate = [e for e in enriched_list if e.segment == "corporate"]
    corporate.sort(key=lambda e: e.profile.avg_ticket, reverse=True)

    banquet = [e for e in enriched_list if e.segment == "banquet"]
    banquet.sort(key=lambda e: e.order_stats.max_ticket, reverse=True)

    lapse_alerts: list[LapseAlert] = []
    for e in enriched_list:
        if e.segment in ("high_value", "corporate", "banquet", "regular"):
            alert = _build_lapse_alert(e, venue_name)
            if alert:
                lapse_alerts.append(alert)
    lapse_alerts.sort(
        key=lambda a: ({"critical": 0, "high": 1, "medium": 2}[a.urgency], -a.monthly_value),
    )

    incentives: list[CustomerIncentive] = []
    for e in enriched_list:
        incentives.extend(_build_incentives(e, venue_name, rules))
    incentives.sort(key=lambda i: i.priority)

    tier_counts = Counter(e.loyalty_tier for e in enriched_list)
    qr_linked = sum(1 for e in enriched_list if e.qr_linked)
    at_risk = sum(1 for e in enriched_list if e.profile.is_lapsing)
    lapsed = sum(1 for e in enriched_list if e.profile.is_lapsed_regular)

    loyalty = LoyaltyProgramSummary(
        total_members=len(enriched_list),
        qr_linked_members=qr_linked,
        tier_bronze=tier_counts.get("bronze", 0),
        tier_silver=tier_counts.get("silver", 0),
        tier_gold=tier_counts.get("gold", 0),
        tier_platinum=tier_counts.get("platinum", 0),
        active_regulars=retention.regular_count - retention.lapsed_regular_count,
        at_risk_count=at_risk,
        lapsed_count=lapsed,
    )

    winback_at_risk = sum(
        e.profile.winback_value for e in enriched_list
        if e.segment in ("high_value", "corporate", "banquet", "regular")
        and (e.profile.is_lapsing or e.profile.is_lapsed_regular)
    )

    sendable = [i for i in incentives if i.phone]

    return CustomerAgentReport(
        venue_name=venue_name,
        period_days=period_days,
        loyalty=loyalty,
        customers=enriched_list,
        high_value=high_value[:25],
        corporate_accounts=corporate,
        banquet_accounts=banquet,
        lapse_alerts=lapse_alerts,
        incentives=incentives,
        total_winback_at_risk=winback_at_risk,
        messages_ready=len(sendable),
    )


def get_incentives_for_customer(
    report: CustomerAgentReport,
    customer_ref: str,
) -> list[CustomerIncentive]:
    """Return draft incentives for a single guest."""
    return [i for i in report.incentives if i.customer_ref == customer_ref]


def get_customer_profile(
    report: CustomerAgentReport,
    customer_ref: str,
) -> EnrichedCustomer | None:
    """Return enriched profile for a single guest."""
    for c in report.customers:
        if c.profile.customer_ref == customer_ref:
            return c
    return None


def link_qr_scan(
    registry: dict[str, LoyaltyCustomer],
    qr_token: str,
    customer_ref: str,
    display_name: str = "",
    phone: str = "",
    channel: str = "sms",
) -> LoyaltyCustomer:
    """Register or update a QR scan — ties token to POS customer_ref."""
    entry = LoyaltyCustomer(
        customer_ref=customer_ref,
        qr_token=qr_token,
        display_name=display_name,
        phone=phone,
        channel=channel,
        opted_in=True,
    )
    registry[customer_ref] = entry
    return entry
