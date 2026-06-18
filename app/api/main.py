"""FastAPI service — QR guest registration and merchant Customer Agent control."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.agents.customer import link_qr_scan, run_customer_agent
from app.api.deps import (
    JOIN_BASE_URL,
    VENUE_NAME,
    VENUE_SLUG,
    build_merchant_dashboard,
    get_message_dispatcher,
    load_orders,
    load_registry,
    load_rules,
    registry_path,
    venues_path,
)
from app.api.merchant import router as merchant_router
from app.ingest.loader import save_customers
from app.report.guest_join_render import render_join_page
from app.services.guest import join_guest, load_venues, recognize_guest

app = FastAPI(title="Asaan Intelligence API", version="0.1.0")
app.include_router(merchant_router)


class QRScanRequest(BaseModel):
    qr_token: str
    customer_ref: str
    display_name: str = ""
    phone: str = ""
    channel: str = Field(default="whatsapp", pattern="^(sms|whatsapp)$")
    opted_in: bool = True


class QRScanResponse(BaseModel):
    customer_ref: str
    qr_token: str
    display_name: str
    registered: bool


class QRJoinRequest(BaseModel):
    venue_slug: str
    display_name: str
    phone: str
    channel: str = Field(default="whatsapp", pattern="^(sms|whatsapp)$")
    opted_in: bool = True


class DispatchRequest(BaseModel):
    customer_refs: list[str] = Field(min_length=1)


def _get_venue(slug: str):
    venues = load_venues(venues_path())
    if slug not in venues:
        raise HTTPException(status_code=404, detail=f"Venue '{slug}' not found")
    return venues[slug]


def _join_result_dict(result) -> dict:
    return asdict(result)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "venue": VENUE_NAME,
        "join_url": f"{JOIN_BASE_URL.rstrip('/')}/join/{VENUE_SLUG}",
    }


@app.get("/join/{venue_slug}", response_class=HTMLResponse)
def join_page_get(venue_slug: str, phone: str = "", lookup: str = ""):
    """Permanent venue QR landing page — guest onboarding and return lookup."""
    venue = _get_venue(venue_slug)
    join_result = None
    if phone and lookup:
        orders, _, _ = load_orders()
        registry = load_registry()
        rules = load_rules()
        result = recognize_guest(venue, registry, phone, orders, rules)
        if result:
            join_result = _join_result_dict(result)
    return render_join_page(venue.name, venue_slug, join_result=join_result)


@app.post("/join/{venue_slug}", response_class=HTMLResponse)
async def join_page_post(
    venue_slug: str,
    display_name: str = Form(...),
    phone: str = Form(...),
    opted_in: str = Form(default=""),
):
    """Handle guest join form from permanent venue QR."""
    venue = _get_venue(venue_slug)
    orders, _, _ = load_orders()
    registry = load_registry()
    rules = load_rules()
    try:
        result = join_guest(
            venue, registry, display_name, phone, orders, rules,
            opted_in=opted_in.lower() in ("true", "1", "on", "yes"),
            channel="whatsapp",
        )
        save_customers(registry_path(), registry)
        return render_join_page(venue.name, venue_slug, join_result=_join_result_dict(result))
    except Exception as e:
        return render_join_page(venue.name, venue_slug, error=str(e))


@app.post("/qr/join")
def qr_join(req: QRJoinRequest):
    """JSON API for guest onboarding from permanent venue QR."""
    venue = _get_venue(req.venue_slug)
    orders, _, _ = load_orders()
    registry = load_registry()
    rules = load_rules()
    result = join_guest(
        venue, registry, req.display_name, req.phone, orders, rules,
        opted_in=req.opted_in, channel=req.channel,
    )
    save_customers(registry_path(), registry)
    return _join_result_dict(result)


@app.get("/qr/recognize")
def qr_recognize(phone: str, venue_slug: str = "sugar-rush"):
    """Look up returning guest by phone."""
    venue = _get_venue(venue_slug)
    orders, _, _ = load_orders()
    registry = load_registry()
    rules = load_rules()
    result = recognize_guest(venue, registry, phone, orders, rules)
    if result is None:
        raise HTTPException(status_code=404, detail="Guest not found")
    return _join_result_dict(result)


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
    venues = load_venues(venues_path())
    default_slug = next(iter(venues), "sugar-rush")
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
        "join_url": f"{JOIN_BASE_URL}/join/{default_slug}",
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
