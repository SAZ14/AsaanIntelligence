"""FastAPI service — QR scan registration and incentive message dispatch."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.agents.customer import link_qr_scan, run_customer_agent
from app.ingest import load_dataset
from app.ingest.loader import load_customers, save_customers
from app.models.canonical import LoyaltyCustomer
from app.services.messaging import ConsoleMessageDispatcher, DispatchReport, FileOutboxDispatcher, dispatch_incentives

def _data_dir() -> Path:
    return Path(os.environ.get(
        "ASAAN_DATA_DIR",
        Path(__file__).resolve().parent.parent.parent / "data",
    ))


def _outbox_dir() -> Path:
    return Path(os.environ.get(
        "ASAAN_OUTBOX_DIR",
        Path(__file__).resolve().parent.parent.parent / "output",
    ))


VENUE_NAME = os.environ.get("ASAAN_VENUE_NAME", "Sugar Rush")

app = FastAPI(title="Asaan Intelligence API", version="0.1.0")


def _registry_path() -> Path:
    return _data_dir() / "customers.csv"


def _load_registry() -> dict[str, LoyaltyCustomer]:
    return load_customers(_registry_path())


def _load_orders():
    d = _data_dir()
    return load_dataset(
        d / "sales_detail.csv",
        d / "menu.csv",
        d / "staff.csv",
    )


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
    limit: int | None = Field(default=10, ge=1, le=100)
    require_phone: bool = True


class DispatchResponse(BaseModel):
    sent: int
    skipped: int
    failed: int
    messages: list[dict]


@app.get("/health")
def health():
    return {"status": "ok", "venue": VENUE_NAME}


@app.post("/qr/scan", response_model=QRScanResponse)
def qr_scan(req: QRScanRequest):
    """Register or update a guest from a QR scan — persists to customers.csv."""
    registry = _load_registry()
    entry = link_qr_scan(
        registry,
        qr_token=req.qr_token,
        customer_ref=req.customer_ref,
        display_name=req.display_name,
        phone=req.phone,
        channel=req.channel,
    )
    entry.opted_in = req.opted_in
    save_customers(_registry_path(), registry)
    return QRScanResponse(
        customer_ref=entry.customer_ref,
        qr_token=entry.qr_token,
        display_name=entry.display_name or f"Guest {entry.customer_ref[-4:].upper()}",
        registered=True,
    )


@app.get("/qr/lookup/{qr_token}")
def qr_lookup(qr_token: str):
    """Resolve a QR token to a customer profile."""
    registry = _load_registry()
    for c in registry.values():
        if c.qr_token == qr_token:
            return c.model_dump()
    raise HTTPException(status_code=404, detail="QR token not found")


@app.post("/messages/dispatch", response_model=DispatchResponse)
def messages_dispatch(req: DispatchRequest):
    """Run Customer Agent and dispatch ready incentive messages."""
    orders, menu, staff = _load_orders()
    registry = _load_registry()
    report = run_customer_agent(orders, menu, staff, registry, venue_name=VENUE_NAME)

    outbox = _outbox_dir() / "messages_outbox.jsonl"
    dispatcher = FileOutboxDispatcher(outbox, ConsoleMessageDispatcher())
    result: DispatchReport = dispatch_incentives(
        report.incentives,
        dispatcher,
        require_phone=req.require_phone,
        limit=req.limit,
    )

    all_msgs = result.sent + result.skipped + result.failed
    return DispatchResponse(
        sent=len(result.sent),
        skipped=len(result.skipped),
        failed=len(result.failed),
        messages=[{
            "customer_ref": m.customer_ref,
            "display_name": m.display_name,
            "channel": m.channel,
            "phone": m.phone,
            "incentive_type": m.incentive_type,
            "status": m.status,
            "message": m.message[:200],
        } for m in all_msgs],
    )


@app.get("/customer/report/summary")
def customer_report_summary():
    """Return Customer Agent summary JSON for dashboards."""
    orders, menu, staff = _load_orders()
    registry = _load_registry()
    report = run_customer_agent(orders, menu, staff, registry, venue_name=VENUE_NAME)
    return {
        "venue_name": report.venue_name,
        "tagline": report.tagline,
        "period_days": report.period_days,
        "loyalty": report.loyalty.__dict__,
        "lapse_alerts_count": len(report.lapse_alerts),
        "incentives_count": len(report.incentives),
        "messages_ready": report.messages_ready,
        "total_winback_at_risk": report.total_winback_at_risk,
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
