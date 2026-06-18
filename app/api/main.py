"""FastAPI service — QR guest registration and merchant Customer Agent control."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.agents.customer import link_qr_scan, run_customer_agent
from app.api.deps import (
    VENUE_NAME,
    build_merchant_dashboard,
    load_orders,
    load_registry,
    load_rules,
    registry_path,
)
from app.api.merchant import router as merchant_router
from app.ingest.loader import save_customers

app = FastAPI(title="Asaan Intelligence API", version="0.1.0")
app.include_router(merchant_router)


class QRScanRequest(BaseModel):
    qr_token: str
    customer_ref: str
    display_name: str = ""
    phone: str = ""
    channel: str = Field(default="sms", pattern="^(sms|whatsapp)$")
    opted_in: bool = True


class QRScanResponse(BaseModel):
    customer_ref: str
    qr_token: str
    display_name: str
    registered: bool


class DispatchRequest(BaseModel):
    customer_refs: list[str] = Field(min_length=1)


@app.get("/health")
def health():
    return {"status": "ok", "venue": VENUE_NAME}


@app.post("/qr/scan", response_model=QRScanResponse)
def qr_scan(req: QRScanRequest):
    """Register or update a guest from a QR scan — persists to customers.csv."""
    registry = load_registry()
    entry = link_qr_scan(
        registry,
        qr_token=req.qr_token,
        customer_ref=req.customer_ref,
        display_name=req.display_name,
        phone=req.phone,
        channel=req.channel,
    )
    entry.opted_in = req.opted_in
    save_customers(registry_path(), registry)
    return QRScanResponse(
        customer_ref=entry.customer_ref,
        qr_token=entry.qr_token,
        display_name=entry.display_name or f"Guest {entry.customer_ref[-4:].upper()}",
        registered=True,
    )


@app.get("/qr/lookup/{qr_token}")
def qr_lookup(qr_token: str):
    """Resolve a QR token to a customer profile."""
    registry = load_registry()
    for c in registry.values():
        if c.qr_token == qr_token:
            return c.model_dump()
    raise HTTPException(status_code=404, detail="QR token not found")


@app.post("/messages/dispatch")
def messages_dispatch_deprecated(req: DispatchRequest):
    """Deprecated — use POST /merchant/incentives/approve instead."""
    from app.agents.merchant_customer import approve_and_send
    from app.api.deps import outbox_path

    dash = build_merchant_dashboard()
    sent, skipped = approve_and_send(dash, req.customer_refs, outbox_path())
    return {
        "deprecated": True,
        "use_instead": "POST /merchant/incentives/approve",
        "sent": len(sent),
        "skipped": len(skipped),
    }


@app.get("/customer/report/summary")
def customer_report_summary():
    """Return Customer Agent summary JSON (legacy guest-facing dashboard)."""
    orders, menu, staff = load_orders()
    registry = load_registry()
    rules = load_rules()
    report = run_customer_agent(
        orders, menu, staff, registry, venue_name=VENUE_NAME, rules=rules,
    )
    return {
        "venue_name": report.venue_name,
        "tagline": report.tagline,
        "period_days": report.period_days,
        "loyalty": report.loyalty.__dict__,
        "lapse_alerts_count": len(report.lapse_alerts),
        "incentives_count": len(report.incentives),
        "messages_ready": report.messages_ready,
        "total_winback_at_risk": report.total_winback_at_risk,
        "merchant_dashboard": "/merchant/dashboard",
        "top_lapse_alerts": [
            {
                "customer_ref": a.customer_ref,
                "display_name": a.display_name,
                "urgency": a.urgency,
                "monthly_value": a.monthly_value,
                "message": a.message,
            }
            for a in report.lapse_alerts[:5]
        ],
    }
