from __future__ import annotations
import logging
import time

from app.config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM

logger = logging.getLogger(__name__)

MAX_CHUNK = 1500


def chunk(text: str, size: int = MAX_CHUNK) -> list[str]:
    """Split text into ≤size char chunks on line/sentence boundaries."""
    if len(text) <= size:
        return [text]

    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= size:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n", 0, size)
        if cut <= 0:
            cut = remaining.rfind(". ", 0, size)
            if cut <= 0:
                cut = size
            else:
                cut += 1
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return [p for p in parts if p]


def send_whatsapp(to: str, text: str) -> None:
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        logger.warning("Twilio not configured — cannot send WhatsApp message")
        return

    from twilio.rest import Client
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

    chunks = chunk(text)
    logger.info("Sending %d chunk(s) to %s", len(chunks), to)

    for i, part in enumerate(chunks):
        try:
            msg = client.messages.create(
                from_=TWILIO_WHATSAPP_FROM,
                to=to,
                body=part,
            )
            logger.info("Sent chunk %d/%d: SID %s", i + 1, len(chunks), msg.sid)
        except Exception as exc:
            logger.error("Failed to send chunk %d: %s. Retrying once.", i + 1, exc)
            try:
                time.sleep(2)
                client.messages.create(from_=TWILIO_WHATSAPP_FROM, to=to, body=part)
            except Exception as exc2:
                logger.error("Retry also failed for chunk %d: %s", i + 1, exc2)
        if i < len(chunks) - 1:
            time.sleep(0.5)
