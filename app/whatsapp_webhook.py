"""FastAPI webhook for inbound Twilio WhatsApp messages.

Exposes POST /whatsapp/incoming which Twilio calls for each inbound WhatsApp
message. It logs the sender + message and replies with TwiML.

Because the Instagram report takes ~30-60s to build (Apify scrape) and Twilio
drops a webhook response it hasn't received within ~15s, report requests are
handled asynchronously: we reply immediately with an acknowledgement, then
generate the report in a background task and push it back as a separate
outbound WhatsApp message via the Twilio REST API.

Run locally:
    uvicorn app.whatsapp_webhook:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
from xml.sax.saxutils import escape

from fastapi import BackgroundTasks, FastAPI, Request, Response

from app.instagram_intel import WHATSAPP_CHAR_LIMIT, generate_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("whatsapp_webhook")

app = FastAPI(title="Asaan Intelligence WhatsApp Webhook")

# Phrases that trigger the full Instagram reputation report.
REPORT_TRIGGERS = (
    "bad comments",
    "bad reviews",
    "check instagram",
    "anatummy",
    "instagram",
)

ACK_MESSAGE = (
    "On it — pulling your Anatummy Instagram report now ⏳ "
    "It'll arrive here in under a minute."
)


def _wa(addr: str) -> str:
    """Ensure a WhatsApp address has the whatsapp: prefix."""
    addr = (addr or "").strip()
    if not addr:
        return ""
    return addr if addr.startswith("whatsapp:") else f"whatsapp:{addr}"


def _twiml(message: str) -> Response:
    """Wrap a message in TwiML and return it as text/xml."""
    if len(message) > WHATSAPP_CHAR_LIMIT:
        message = message[: WHATSAPP_CHAR_LIMIT - 3].rstrip() + "..."
    body = escape(message)  # escape XML special characters
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{body}</Message></Response>"
    )
    return Response(content=xml, media_type="text/xml")


def send_whatsapp(to_addr: str, from_addr: str, body: str) -> None:
    """Send an outbound WhatsApp message via the Twilio REST API."""
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth = os.environ.get("TWILIO_AUTH_TOKEN")
    if not (sid and auth):
        logger.error("Cannot send outbound WhatsApp: Twilio credentials missing")
        return
    if not (to_addr and from_addr):
        logger.error("Cannot send outbound WhatsApp: missing to/from address")
        return
    if len(body) > WHATSAPP_CHAR_LIMIT:
        body = body[: WHATSAPP_CHAR_LIMIT - 3].rstrip() + "..."

    from twilio.rest import Client

    client = Client(sid, auth)
    msg = client.messages.create(body=body, from_=from_addr, to=to_addr)
    logger.info("Outbound report sent to %s (SID %s, status %s)",
                to_addr, msg.sid, msg.status)


def generate_and_send_report(to_addr: str, from_addr: str) -> None:
    """Background task: build the Instagram report and push it to the user."""
    try:
        report = generate_report()
    except Exception as e:  # noqa: BLE001
        logger.exception("Report generation failed")
        report = (
            "Sorry — I couldn't pull the Instagram report just now "
            f"({type(e).__name__}). Please try again shortly."
        )
    try:
        send_whatsapp(to_addr, from_addr, report)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to send report to %s", to_addr)


@app.get("/")
def root() -> dict[str, str]:
    return {"status": "ok", "endpoint": "POST /whatsapp/incoming"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/whatsapp/incoming")
async def whatsapp_incoming(
    request: Request, background_tasks: BackgroundTasks
) -> Response:
    # 1-2. Accept Twilio form-encoded POST and read From / Body (and To).
    form = await request.form()
    sender = (form.get("From") or "").strip()
    inbound_to = (form.get("To") or "").strip()
    body = (form.get("Body") or "").strip()

    # 3. Log the sender and message.
    logger.info("Inbound WhatsApp from %s: %r", sender or "(unknown)", body)

    lowered = body.lower()
    if any(trigger in lowered for trigger in REPORT_TRIGGERS):
        # Reply to the user (= inbound From) from the sandbox number that received
        # the message (= inbound To), falling back to the configured sender.
        reply_to = _wa(sender)
        reply_from = _wa(
            inbound_to
            or os.environ.get("TWILIO_WHATSAPP_NUMBER")
            or os.environ.get("TWILIO_WHATSAPP_FROM", "")
        )
        logger.info("Trigger matched -> scheduling report for %s (from %s)",
                    reply_to, reply_from)
        # 5. Generate the report asynchronously and push it back; ack immediately
        #    so we stay well within Twilio's ~15s webhook timeout.
        background_tasks.add_task(generate_and_send_report, reply_to, reply_from)
        reply = ACK_MESSAGE
    else:
        # 4. Default echo reply.
        reply = f"Asaan Intelligence received: {body}"

    # 6-7. Keep under the limit and return TwiML as text/xml.
    return _twiml(reply)
