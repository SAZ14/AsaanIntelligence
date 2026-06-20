"""Inbound WhatsApp webhook — turns each wa.me message into a loyalty scan.

The venue QR encodes a wa.me click-to-chat link (see
``app.agents.customer.build_wa_link``). When the customer sends the pre-filled
message, Twilio POSTs it to ``/whatsapp/inbound``; we register one scan and
reply via TwiML, so the progress message lands straight back in the customer's
WhatsApp chat.

The customer's WhatsApp number (Twilio's ``From`` field) is the only identity —
no signup, no name, no payment token.

Run locally:
    uvicorn app.whatsapp.webhook:app --port 8000
    # expose with ngrok and paste the URL into the Twilio sandbox "when a
    # message comes in" webhook field.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import parse_qs
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request, Response

from app.agents.customer import JsonCardStore, LoyaltyProgram

# Persistent so stamps survive restarts; path/venue come from the environment.
STORE_PATH = Path(os.environ.get("LOYALTY_STORE", "loyalty_cards.json"))
VENUE_NAME = os.environ.get("VENUE_NAME", "Sugar Rush")

app = FastAPI(title="AsaanPay Loyalty webhook")
program = LoyaltyProgram(venue_name=VENUE_NAME, store=JsonCardStore(STORE_PATH))


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


@app.post("/whatsapp/inbound")
async def inbound(request: Request) -> Response:
    # Parse the urlencoded body directly so python-multipart isn't required.
    raw = (await request.body()).decode("utf-8", "ignore")
    form = parse_qs(raw)
    from_number = (form.get("From", [""])[0])
    reply = process_scan(from_number, program)
    return Response(content=twiml_reply(reply), media_type="application/xml")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "venue": VENUE_NAME}
