"""Merchant Customer Agent — control layer for guest loyalty and communications.

Wraps run_customer_agent(); does not duplicate intelligence. Merchants review
draft messages in an inbox and approve before anything is sent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.agents.customer import (
    CustomerAgentReport,
    CustomerIncentive,
    EnrichedCustomer,
    LapseAlert,
    TAGLINE,
    get_customer_profile,
    get_incentives_for_customer,
    run_customer_agent,
)
from app.models.canonical import LoyaltyCustomer, LoyaltyRules, MenuItem, Order, Staff
from app.services.messaging import (
    FileOutboxDispatcher,
    SentMessage,
    dispatch_incentives,
    get_dispatcher,
)


@dataclass
class MerchantHeadlines:
    venue_name: str = ""
    tagline: str = TAGLINE
    period_days: int = 0
    lapsed_count: int = 0
    at_risk_count: int = 0
    winback_at_risk: float = 0.0
    qr_linked: int = 0
    pending_approval: int = 0
    ready_to_send: int = 0
    needs_qr_link: int = 0


@dataclass
class PendingComm:
    customer_ref: str
    display_name: str
    incentive_type: str
    reward_text: str
    message: str
    channel: str
    phone: str
    priority: int
    trigger_reason: str
    sendable: bool
    block_reason: str = ""


@dataclass
class GuestSummary:
    customer_ref: str
    display_name: str
    segment: str
    loyalty_tier: str
    visit_count: int
    total_spend: float
    days_since_last: int
    monthly_value: float
    qr_linked: bool
    opted_in: bool
    status: str  # active | lapsing | lapsed


@dataclass
class MerchantCustomerDashboard:
    headlines: MerchantHeadlines = field(default_factory=MerchantHeadlines)
    inbox: list[LapseAlert] = field(default_factory=list)
    guests: list[GuestSummary] = field(default_factory=list)
    pending_comms: list[PendingComm] = field(default_factory=list)
    sent_comms: list[SentMessage] = field(default_factory=list)
    rules: LoyaltyRules = field(default_factory=LoyaltyRules)
    report: CustomerAgentReport | None = None


def _guest_status(ec: EnrichedCustomer) -> str:
    if ec.profile.is_lapsed_regular:
        return "lapsed"
    if ec.profile.is_lapsing:
        return "lapsing"
    return "active"


def _to_guest_summary(ec: EnrichedCustomer) -> GuestSummary:
    p = ec.profile
    return GuestSummary(
        customer_ref=p.customer_ref,
        display_name=ec.display_name,
        segment=ec.segment,
        loyalty_tier=ec.loyalty_tier,
        visit_count=p.visit_count,
        total_spend=p.total_spend,
        days_since_last=p.days_since_last,
        monthly_value=ec.monthly_value,
        qr_linked=ec.qr_linked,
        opted_in=ec.opted_in,
        status=_guest_status(ec),
    )


def _to_pending(inc: CustomerIncentive, ec: EnrichedCustomer | None) -> PendingComm:
    sendable = bool(inc.phone) and (ec is None or not ec.qr_linked or ec.opted_in)
    block = ""
    if not inc.phone:
        block = "needs QR link"
    elif ec and ec.qr_linked and not ec.opted_in:
        block = "opted out"
    return PendingComm(
        customer_ref=inc.customer_ref,
        display_name=inc.display_name,
        incentive_type=inc.incentive_type,
        reward_text=inc.reward_text,
        message=inc.message,
        channel=inc.channel,
        phone=inc.phone,
        priority=inc.priority,
        trigger_reason=inc.trigger_reason,
        sendable=sendable,
        block_reason=block,
    )


def load_comms_history(outbox_path: Path) -> list[SentMessage]:
    if not outbox_path.exists():
        return []
    history: list[SentMessage] = []
    with open(outbox_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            history.append(SentMessage(**data))
    return history


def filter_guests(
    guests: list[GuestSummary],
    segment: str | None = None,
    tier: str | None = None,
    status: str | None = None,
) -> list[GuestSummary]:
    out = guests
    if segment:
        out = [g for g in out if g.segment == segment]
    if tier:
        out = [g for g in out if g.loyalty_tier == tier]
    if status:
        out = [g for g in out if g.status == status]
    return out


def run_merchant_customer_agent(
    orders: list[Order],
    menu: dict[str, MenuItem],
    staff: dict[str, Staff],
    registry: dict[str, LoyaltyCustomer] | None = None,
    rules: LoyaltyRules | None = None,
    venue_name: str = "your venue",
    outbox_path: Path | None = None,
) -> MerchantCustomerDashboard:
    """Build merchant dashboard: inbox, guest list, pending comms queue."""
    rules = rules or LoyaltyRules()
    report = run_customer_agent(
        orders, menu, staff, registry, venue_name=venue_name, rules=rules,
    )

    guest_map = {c.profile.customer_ref: c for c in report.customers}
    pending: list[PendingComm] = []
    ready = 0
    needs_link = 0
    for inc in sorted(report.incentives, key=lambda i: i.priority):
        ec = guest_map.get(inc.customer_ref)
        pc = _to_pending(inc, ec)
        pending.append(pc)
        if pc.sendable:
            ready += 1
        elif pc.block_reason == "needs QR link":
            needs_link += 1

    headlines = MerchantHeadlines(
        venue_name=report.venue_name,
        tagline=report.tagline,
        period_days=report.period_days,
        lapsed_count=report.loyalty.lapsed_count,
        at_risk_count=report.loyalty.at_risk_count,
        winback_at_risk=report.total_winback_at_risk,
        qr_linked=report.loyalty.qr_linked_members,
        pending_approval=len(pending),
        ready_to_send=ready,
        needs_qr_link=needs_link,
    )

    sent = load_comms_history(outbox_path) if outbox_path else []

    return MerchantCustomerDashboard(
        headlines=headlines,
        inbox=list(report.lapse_alerts),
        guests=[_to_guest_summary(c) for c in report.customers],
        pending_comms=pending,
        sent_comms=sent,
        rules=rules,
        report=report,
    )


def approve_and_send(
    dashboard: MerchantCustomerDashboard,
    customer_refs: list[str],
    outbox_path: Path,
) -> tuple[list[SentMessage], list[PendingComm]]:
    """Approve selected drafts and dispatch one message per guest (highest priority)."""
    if dashboard.report is None:
        return [], []

    ref_set = set(customer_refs)
    pending_by_ref: dict[str, list[PendingComm]] = {}
    for p in dashboard.pending_comms:
        pending_by_ref.setdefault(p.customer_ref, []).append(p)

    to_send: list[CustomerIncentive] = []
    for cref in customer_refs:
        if cref not in ref_set:
            continue
        pending_for_guest = pending_by_ref.get(cref, [])
        sendable = [p for p in pending_for_guest if p.sendable]
        if not sendable:
            continue
        best_type = min(sendable, key=lambda p: p.priority).incentive_type
        for inc in dashboard.report.incentives:
            if inc.customer_ref == cref and inc.incentive_type == best_type:
                to_send.append(inc)
                break

    dispatcher = FileOutboxDispatcher(outbox_path, get_dispatcher())
    result = dispatch_incentives(to_send, dispatcher, require_phone=True)

    skipped_pending = [
        p for cref in ref_set for p in pending_by_ref.get(cref, [])
        if not p.sendable
    ]
    return result.sent, skipped_pending


def get_guest_detail(
    dashboard: MerchantCustomerDashboard,
    customer_ref: str,
) -> dict | None:
    if dashboard.report is None:
        return None
    ec = get_customer_profile(dashboard.report, customer_ref)
    if ec is None:
        return None
    incentives = get_incentives_for_customer(dashboard.report, customer_ref)
    pending = [p for p in dashboard.pending_comms if p.customer_ref == customer_ref]
    return {
        "guest": _to_guest_summary(ec),
        "favorite_items": ec.order_stats.favorite_items,
        "preferred_channel": ec.order_stats.preferred_channel,
        "pending_comms": pending,
        "incentives": [
            {
                "incentive_type": i.incentive_type,
                "reward_text": i.reward_text,
                "message": i.message,
                "channel": i.channel,
                "phone": i.phone,
                "priority": i.priority,
                "trigger_reason": i.trigger_reason,
            }
            for i in incentives
        ],
    }
