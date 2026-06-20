"""FastAPI webhook for inbound Twilio WhatsApp messages.

Exposes POST /whatsapp/incoming which Twilio calls for each inbound WhatsApp
message. It logs the sender + message and replies with TwiML. If the message
asks about the Anatummy Instagram reputation (e.g. "check instagram",
"bad reviews", "anatummy"), it runs the Apify + reputation agent flow and
returns the report as the reply.

Run locally:
    uvicorn app.whatsapp_webhook:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request, Response

from app.instagram_intel import WHATSAPP_CHAR_LIMIT, generate_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("whatsapp_webhook")

app = FastAPI(title="Asaan Intelligence WhatsApp Webhook")

# Phrases that trigger the full Instagram reputation report.
REPORT_TRIGGERS = ("bad comments", "bad reviews", "check instagram", "anatummy")


def _twiml(message: str) -> Response:
    """Wrap a message in TwiML and return it as text/xml."""
    if len(message) > WHATSAPP_CHAR_LIMIT:
        message = message[: WHATSAPP_CHAR_LIMIT - 3].rstrip() + "..."
    # Escape XML special characters in the message body.
    body = escape(message)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{body}</Message></Response>"
    )
    return Response(content=xml, media_type="text/xml")


@app.get("/")
def health() -> dict[str, str]:
    return {"status": "ok", "endpoint": "POST /whatsapp/incoming"}


@app.post("/whatsapp/incoming")
async def whatsapp_incoming(request: Request) -> Response:
    # 1-2. Accept Twilio form-encoded POST and read From / Body.
    form = await request.form()
    sender = (form.get("From") or "").strip()
    body = (form.get("Body") or "").strip()

    # 3. Log the sender and message.
    logger.info("Inbound WhatsApp from %s: %r", sender or "(unknown)", body)

    lowered = body.lower()
    if any(trigger in lowered for trigger in REPORT_TRIGGERS):
        # 5. Run the Anatummy Apify + reputation agent flow.
        logger.info("Trigger matched -> generating Anatummy reputation report")
        try:
            reply = generate_report()
        except Exception as e:  # noqa: BLE001
            logger.exception("Report generation failed")
            reply = (
                "Sorry — I couldn't pull the Instagram report just now "
                f"({type(e).__name__}). Please try again shortly."
            )
    else:
        # 4. Default echo reply.
        reply = f"Asaan Intelligence received: {body}"

    # 6-7. Keep under the limit and return TwiML as text/xml.
    return _twiml(reply)
