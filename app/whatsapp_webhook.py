"""FastAPI webhook for inbound Twilio WhatsApp messages.

Two-message delivery pattern (so we never hit Twilio's ~15s webhook timeout):

  1. POST /whatsapp/incoming returns an IMMEDIATE TwiML ack. It never runs the
     LLM/Apify work, so the response goes back in milliseconds.
  2. The agent (Apify scrape + reputation report) runs in a detached background
     thread, fully decoupled from the request/response cycle.
  3. When the agent finishes, the result is sent back as a NEW outbound WhatsApp
     message via the Twilio REST API (the "notifier") — not as the webhook's
     return value. Long reports are split into multiple WhatsApp messages.

Run locally:
    uvicorn app.whatsapp_webhook:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
import threading
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
REPORT_TRIGGERS = (
    "bad comments",
    "bad reviews",
    "check instagram",
    "anatummy",
    "instagram",
)

ACK_MESSAGE = (
    "On it — pulling your Anatummy Instagram report now ⏳ "
    "It'll arrive here in a few seconds."
)


# ── Address + TwiML helpers ──

def _wa(addr: str) -> str:
    """Ensure a WhatsApp address has the whatsapp: prefix."""
    addr = (addr or "").strip()
    if not addr:
        return ""
    return addr if addr.startswith("whatsapp:") else f"whatsapp:{addr}"


def _twiml(message: str) -> Response:
    """Wrap a short message in TwiML and return it as text/xml (the ack)."""
    if len(message) > WHATSAPP_CHAR_LIMIT:
        message = message[: WHATSAPP_CHAR_LIMIT - 3].rstrip() + "..."
    body = escape(message)  # escape XML special characters
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{body}</Message></Response>"
    )
    return Response(content=xml, media_type="text/xml")


# ── Outbound notifier (Twilio REST) ──

def _split_message(text: str, limit: int = WHATSAPP_CHAR_LIMIT) -> list[str]:
    """Split a long message into <=limit-char parts on line boundaries.

    When more than one part is produced, each is prefixed with "(i/N) " so the
    recipient can follow the order.
    """
    text = text.strip()
    if len(text) <= limit:
        return [text]

    eff = limit - 8  # leave room for the "(i/N) " prefix
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        piece = line + "\n"
        # A single line longer than the budget: hard-split it.
        while len(piece) > eff:
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""
            chunks.append(piece[:eff])
            piece = piece[eff:]
        if len(current) + len(piece) > eff:
            chunks.append(current.rstrip("\n"))
            current = ""
        current += piece
    if current.strip():
        chunks.append(current.rstrip("\n"))

    n = len(chunks)
    if n > 1:
        chunks = [f"({i}/{n}) {c}" for i, c in enumerate(chunks, 1)]
    return chunks


def _classify_twilio_error(code, status, msg: str) -> str:
    m = (msg or "").lower()
    if status == 401 or code == 20003:
        return "auth — Twilio Account SID / Auth Token rejected"
    if code == 63007 or "channel" in m or "not a valid whatsapp" in m:
        return "wrong sender — 'From' is not a configured WhatsApp sender"
    if code == 63016 or "outside" in m or "session" in m or "24" in m:
        return "24-hour window — recipient must message the sandbox first"
    if code == 63015 or "sandbox" in m or "join" in m:
        return "sandbox enrollment — recipient hasn't joined the sandbox"
    if code in (21211, 21614):
        return "wrong recipient — 'To' invalid or not WhatsApp-enabled"
    return "unclassified — see code/message"


def send_whatsapp(to_addr: str, from_addr: str, body: str) -> bool:
    """Send an outbound WhatsApp message (split if long). Returns True on success."""
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth = os.environ.get("TWILIO_AUTH_TOKEN")
    if not (sid and auth):
        logger.error("Cannot send outbound WhatsApp: Twilio credentials missing")
        return False
    if not (to_addr and from_addr):
        logger.error("Cannot send outbound WhatsApp: to=%r from=%r", to_addr, from_addr)
        return False

    from twilio.rest import Client

    client = Client(sid, auth)
    parts = _split_message(body)
    logger.info("Notifier: sending %d part(s) to %s from %s",
                len(parts), to_addr, from_addr)
    for i, part in enumerate(parts, 1):
        try:
            msg = client.messages.create(body=part, from_=from_addr, to=to_addr)
            logger.info("  part %d/%d -> SID %s status %s",
                        i, len(parts), msg.sid, msg.status)
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", None)
            status = getattr(e, "status", None)
            emsg = getattr(e, "msg", None) or str(e)
            logger.error("  part %d/%d FAILED: code=%s status=%s msg=%s -> %s",
                         i, len(parts), code, status, emsg,
                         _classify_twilio_error(code, status, emsg))
            return False
    return True


# ── Background agent runner ──

def run_agent_and_notify(to_addr: str, from_addr: str) -> None:
    """Detached worker: build the report and push it via the Twilio notifier."""
    logger.info("Background agent START for %s", to_addr)
    try:
        report = generate_report()
        logger.info("Background agent: report built (%d chars) for %s",
                    len(report), to_addr)
    except Exception as e:  # noqa: BLE001
        logger.exception("Background agent: report generation failed")
        report = (
            "Sorry — I couldn't pull the Instagram report just now "
            f"({type(e).__name__}). Please try again shortly."
        )
    ok = send_whatsapp(to_addr, from_addr, report)
    logger.info("Background agent DONE for %s (sent=%s)", to_addr, ok)


# ── Routes ──

@app.get("/")
def root() -> dict[str, str]:
    return {"status": "ok", "endpoint": "POST /whatsapp/incoming"}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/whatsapp/incoming")
async def whatsapp_incoming(request: Request) -> Response:
    # 1-2. Accept Twilio form-encoded POST; read From, To, Body.
    form = await request.form()
    sender = (form.get("From") or "").strip()
    inbound_to = (form.get("To") or "").strip()
    body = (form.get("Body") or "").strip()

    # 3. Log the inbound message.
    logger.info("Inbound WhatsApp from %s to %s: %r",
                sender or "(unknown)", inbound_to or "(unknown)", body)

    lowered = body.lower()
    if any(trigger in lowered for trigger in REPORT_TRIGGERS):
        # Reply to the sender (inbound From), from the sandbox number that
        # received the message (inbound To), falling back to configured sender.
        reply_to = _wa(sender)
        reply_from = _wa(
            inbound_to
            or os.environ.get("TWILIO_WHATSAPP_NUMBER")
            or os.environ.get("TWILIO_WHATSAPP_FROM", "")
        )
        logger.info("Trigger matched -> launching background agent: to=%s from=%s",
                    reply_to, reply_from)
        # Detached thread: fully decoupled from this response so the agent always
        # runs and the ack returns instantly (no LLM/Apify work on this path).
        threading.Thread(
            target=run_agent_and_notify,
            args=(reply_to, reply_from),
            name="agent-notify",
            daemon=True,
        ).start()
        logger.info("Instant ack returned to %s", reply_to)
        return _twiml(ACK_MESSAGE)

    # 4. Non-trigger messages get an immediate echo reply.
    return _twiml(f"Asaan Intelligence received: {body}")
