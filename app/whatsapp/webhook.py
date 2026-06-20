"""Inbound WhatsApp webhook — turns each wa.me message into a loyalty scan.

Multi-restaurant: one deployment serves many venues. Each restaurant has its
own WhatsApp number (its own QR), so we route an inbound message to the right
restaurant by the number it was sent to (Twilio's ``To`` field) and stamp that
restaurant's card. A customer can be a member at several restaurants at once,
each tracked independently.

The customer's WhatsApp number (``From``) is the only customer identity — no
signup, no name, no payment token.

Run locally:
    RESTAURANTS_CONFIG=restaurants.json LOYALTY_DIR=loyalty_data \
        uvicorn app.whatsapp.webhook:app --port 8000
    # expose with ngrok and point each restaurant's Twilio number's
    # "when a message comes in" webhook at /whatsapp/inbound.
"""

from __future__ import annotations

from urllib.parse import parse_qs
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request, Response

from app.agents.customer import LoyaltyProgram, Registry, build_default_registry

app = FastAPI(title="AsaanPay Loyalty webhook")
registry: Registry = build_default_registry()


def twiml_reply(body: str) -> str:
    """Wrap a reply body in TwiML so Twilio sends it back to the sender."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{escape(body)}</Message></Response>"
    )


def process_scan(from_number: str, prog: LoyaltyProgram) -> str:
    """Framework-agnostic core: one inbound message → the reply text."""
    number = from_number.replace("whatsapp:", "").strip()
    if not number:
        return "Sorry, we couldn't read your number — please try scanning again."
    return prog.record_scan(number).message


def route(from_number: str, to_number: str, reg: Registry) -> str:
    """Pick the restaurant from the ``To`` number and stamp its card."""
    restaurant = reg.by_number(to_number)
    if restaurant is None:
        # Single-restaurant deployments may not send a matching To — fall back.
        venues = reg.all()
        restaurant = venues[0] if len(venues) == 1 else None
    if restaurant is None:
        return "This number isn't set up for loyalty rewards yet."
    return process_scan(from_number, restaurant.program)


@app.post("/whatsapp/inbound")
async def inbound(request: Request) -> Response:
    # Parse the urlencoded body directly so python-multipart isn't required.
    raw = (await request.body()).decode("utf-8", "ignore")
    form = parse_qs(raw)
    from_number = form.get("From", [""])[0]
    to_number = form.get("To", [""])[0]
    reply = route(from_number, to_number, registry)
    return Response(content=twiml_reply(reply), media_type="application/xml")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "restaurants": [r.id for r in registry.all()]}
