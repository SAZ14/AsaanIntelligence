"""Merchant Customer Agent API — inbox, guest CRM, approve/send, rules."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.agents.merchant_customer import approve_and_send, filter_guests, get_guest_detail
from app.api.deps import (
    VENUE_NAME,
    build_merchant_dashboard,
    load_registry,
    load_rules,
    outbox_path,
    registry_path,
    save_rules,
)
from app.ingest.loader import save_customers
from app.models.canonical import LoyaltyRules

router = APIRouter(prefix="/merchant", tags=["merchant"])


class ApproveRequest(BaseModel):
    customer_refs: list[str] = Field(min_length=1)


class ApproveResponse(BaseModel):
    sent: int
    skipped: int
    messages: list[dict]


def _headlines_dict(dashboard) -> dict:
    return asdict(dashboard.headlines)


def _inbox_dict(dashboard) -> list[dict]:
    return [asdict(a) for a in dashboard.inbox]


def _pending_dict(dashboard) -> list[dict]:
    return [asdict(p) for p in dashboard.pending_comms]


@router.get("/dashboard")
def merchant_dashboard():
    """Full merchant JSON dashboard."""
    dash = build_merchant_dashboard()
    return {
        "headlines": _headlines_dict(dash),
        "inbox": _inbox_dict(dash),
        "guests_count": len(dash.guests),
        "pending_comms": _pending_dict(dash),
        "sent_comms_count": len(dash.sent_comms),
        "rules": dash.rules.model_dump(),
    }


@router.get("/inbox")
def merchant_inbox():
    """Lapse alerts inbox only."""
    dash = build_merchant_dashboard()
    return {"inbox": _inbox_dict(dash), "count": len(dash.inbox)}


@router.get("/customers")
def merchant_customers(
    segment: str | None = None,
    tier: str | None = None,
    status: str | None = None,
):
    """List guests with optional filters."""
    dash = build_merchant_dashboard()
    guests = filter_guests(dash.guests, segment=segment, tier=tier, status=status)
    return {"guests": [asdict(g) for g in guests], "count": len(guests)}


@router.get("/customers/{customer_ref}")
def merchant_customer_detail(customer_ref: str):
    """Single guest profile + pending drafts."""
    dash = build_merchant_dashboard()
    detail = get_guest_detail(dash, customer_ref)
    if detail is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return detail


@router.get("/incentives/pending")
def merchant_pending_incentives():
    """Draft messages awaiting merchant approval."""
    dash = build_merchant_dashboard()
    return {
        "pending": _pending_dict(dash),
        "ready_to_send": dash.headlines.ready_to_send,
        "needs_qr_link": dash.headlines.needs_qr_link,
    }


@router.post("/incentives/approve", response_model=ApproveResponse)
def merchant_approve_incentives(req: ApproveRequest):
    """Approve and send messages for selected customer_refs."""
    dash = build_merchant_dashboard()
    sent, skipped = approve_and_send(dash, req.customer_refs, outbox_path())
    return ApproveResponse(
        sent=len(sent),
        skipped=len(skipped),
        messages=[asdict(m) for m in sent] + [asdict(s) for s in skipped],
    )


@router.get("/comms/history")
def merchant_comms_history():
    """Sent/skipped message history from outbox."""
    dash = build_merchant_dashboard()
    return {
        "messages": [asdict(m) for m in dash.sent_comms],
        "count": len(dash.sent_comms),
    }


@router.get("/rules")
def merchant_get_rules():
    return load_rules().model_dump()


@router.put("/rules")
def merchant_update_rules(rules: LoyaltyRules):
    save_rules(rules)
    return {"updated": True, "rules": rules.model_dump()}


@router.put("/customers/{customer_ref}/opt-out")
def merchant_opt_out(customer_ref: str):
    """Pause outbound messaging for one guest."""
    registry = load_registry()
    if customer_ref not in registry:
        raise HTTPException(status_code=404, detail="Customer not in QR registry")
    registry[customer_ref].opted_in = False
    save_customers(registry_path(), registry)
    return {"customer_ref": customer_ref, "opted_in": False}
