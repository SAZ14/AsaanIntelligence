"""Twilio webhooks — inbound WhatsApp messages for guest onboarding."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Response

from app.api.deps import (
    VENUE_SLUG,
    load_orders,
    load_registry,
    load_rules,
    registry_path,
    sessions_path,
    venues_path,
)
from app.services.whatsapp_agent import process_and_reply

router = APIRouter(prefix="/webhooks/twilio", tags=["webhooks"])


@router.post("/whatsapp")
async def twilio_whatsapp_inbound(
    From: str = Form(...),
    Body: str = Form(default=""),
    To: str = Form(default=""),
):
    """Twilio inbound WhatsApp webhook — phone from `From`, agent asks for name in chat."""
    if not From:
        raise HTTPException(status_code=400, detail="Missing From")

    orders, _, _ = load_orders()
    registry = load_registry()
    rules = load_rules()

    try:
        process_and_reply(
            From,
            Body,
            venue_slug=VENUE_SLUG,
            venues_path=venues_path(),
            registry=registry,
            orders=orders,
            rules=rules,
            sessions_path=sessions_path(),
            registry_path=registry_path(),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    # Twilio accepts empty TwiML when replies are sent via REST API.
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="application/xml",
    )
