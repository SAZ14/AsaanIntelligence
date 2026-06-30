"""Central Twilio WhatsApp send utility. All outbound messages must go through here."""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1500


def _chunk(text: str, size: int = CHUNK_SIZE) -> list[str]:
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
            cut = (cut + 1) if cut > 0 else size
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return [p for p in parts if p]


def send_whatsapp(to: str, body: str, from_: str, *, retry: bool = True) -> None:
    """Send a WhatsApp message, splitting into chunks if body exceeds 1500 chars."""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    if not account_sid or not auth_token:
        logger.warning("twilio_send: credentials missing — skipping send to %s", to)
        return

    to_addr = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
    from_addr = from_ if from_.startswith("whatsapp:") else f"whatsapp:{from_}"

    from twilio.rest import Client
    client = Client(account_sid, auth_token)
    chunks = _chunk(body)
    labeled = [f"[{i+1}/{len(chunks)}]\n{c}" if len(chunks) > 1 else c for i, c in enumerate(chunks)]

    for i, chunk in enumerate(labeled):
        try:
            msg = client.messages.create(to=to_addr, from_=from_addr, body=chunk)
            logger.info("twilio_send: sent chunk %d/%d sid=%s to=%s", i + 1, len(chunks), msg.sid, to_addr)
        except Exception as exc:
            logger.error("twilio_send: chunk %d/%d failed: %s", i + 1, len(chunks), exc)
            if retry:
                try:
                    time.sleep(2)
                    client.messages.create(to=to_addr, from_=from_addr, body=chunk)
                    logger.info("twilio_send: retry succeeded chunk %d/%d", i + 1, len(chunks))
                except Exception as exc2:
                    logger.error("twilio_send: retry also failed chunk %d/%d: %s", i + 1, len(chunks), exc2)
        if i < len(chunks) - 1:
            time.sleep(0.3)
