"""Asaan Intelligence API — Sugar Rush community agents."""

from __future__ import annotations

from app.api.deps import venue_name
from app.api.staff import router as staff_router
from app.api.webhooks import router as webhooks_router
from app.community.menu_context import build_enroll_qr_url
from app.community.store import load_venue_config
from app.services.messaging import twilio_whatsapp_digits
from fastapi import FastAPI

app = FastAPI(title="Asaan Intelligence API", version="0.2.0")
app.include_router(webhooks_router)
app.include_router(staff_router)


@app.get("/health")
def health():
    config = load_venue_config()
    digits = twilio_whatsapp_digits("TWILIO_WHATSAPP_CUSTOMER_FROM")
    enroll_url = build_enroll_qr_url(digits, config.qr_greeting) if digits else ""
    return {
        "status": "ok",
        "venue": venue_name(),
        "enroll_qr_url": enroll_url,
        "customer_webhook": "/webhooks/twilio/customer",
        "merchant_webhook": "/webhooks/twilio/merchant",
        "receipt_api": "/staff/receipt",
    }
