"""Outbound WhatsApp sender via OpenWA REST API (demo transport).

Replaces twilio_client.py for the demo/openwa branch. Sends text messages and
PDF documents via a locally-running OpenWA gateway.

Environment variables:
    OPENWA_BASE_URL    e.g. "http://localhost:2785/api"
    OPENWA_SESSION_ID  e.g. "default"
    OPENWA_API_KEY     API key from OpenWA dashboard
"""

from __future__ import annotations

import base64
import json
import os
from urllib import request


def _cfg() -> tuple[str, str, str]:
    base = os.environ.get("OPENWA_BASE_URL", "http://localhost:2785/api").rstrip("/")
    session = os.environ.get("OPENWA_SESSION_ID", "default")
    key = os.environ.get("OPENWA_API_KEY", "")
    return base, session, key


def _normalize(number: str) -> str:
    """Convert any number format to OpenWA chatId: digits@c.us."""
    n = number
    if n.startswith("whatsapp:"):
        n = n[len("whatsapp:"):]
    n = n.lstrip("+")
    if not n.endswith("@c.us"):
        n = f"{n}@c.us"
    return n


def _post(url: str, payload: dict, api_key: str) -> dict:
    data = json.dumps(payload).encode()
    req = request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": api_key,
        },
    )
    with request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


def send_text(to_number: str, body: str) -> str:
    """Send a text message. Returns messageId."""
    base, session, key = _cfg()
    url = f"{base}/sessions/{session}/messages/send-text"
    result = _post(url, {"chatId": _normalize(to_number), "text": body}, key)
    return result.get("id", "")


def send_document(
    to_number: str,
    pdf_bytes: bytes,
    filename: str,
    caption: str = "",
) -> str:
    """Send a PDF document inline (base64). Returns messageId."""
    base, session, key = _cfg()
    url = f"{base}/sessions/{session}/messages/send-document"
    payload: dict = {
        "chatId": _normalize(to_number),
        "base64": base64.b64encode(pdf_bytes).decode(),
        "mimetype": "application/pdf",
        "filename": filename,
    }
    if caption:
        payload["caption"] = caption
    result = _post(url, payload, key)
    return result.get("id", "")
