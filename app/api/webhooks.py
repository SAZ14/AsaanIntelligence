"""Twilio webhooks — inbound WhatsApp messages for guest onboarding."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from app.api.deps import (
    VENUE_SLUG,
    load_orders,
    load_registry,
    load_rules,
    public_base_url,
    registry_path,
    sessions_path,
    venues_path,
)
from app.services.twilio_validation import (
    is_valid_twilio_request,
    signature_validation_enabled,
)
from app.services.whatsapp_agent import process_and_reply

router = APIRouter(prefix="/webhooks/twilio", tags=["webhooks"])


def _webhook_url(request: Request) -> str:
    """URL Twilio signed over — the public base (proxies rewrite the host) when set."""
    base = public_base_url()
    if base:
        return base + request.url.path
    return str(request.url)


@router.post("/whatsapp")
async def twilio_whatsapp_inbound(request: Request):
    """Twilio inbound WhatsApp webhook — phone from `From`, agent asks for name in chat."""
    # Twilio signs *all* POST params, so validate over the full form, not just From/Body.
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}

    if signature_validation_enabled():
        signature = request.headers.get("X-Twilio-Signature", "")
        if not is_valid_twilio_request(_webhook_url(request), params, signature):
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    From = params.get("From", "")
    Body = params.get("Body", "")
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
