"""Send WhatsApp messages via the OpenWA REST API.

Mirrors the interface of twilio_send.py so callers can swap providers
by swapping the send function without changing agent code.

Env vars required:
  OPENWA_BASE_URL  – e.g. https://asaan-openwa.up.railway.app
  OPENWA_API_KEY   – API key generated in the OpenWA dashboard
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1500


def _chunk(text: str, size: int = CHUNK_SIZE) -> list[str]:
    from app.core.twilio_send import _chunk as _tc
    return _tc(text, size)


def send_openwa(session_id: str, to_jid: str, body: str, *, retry: bool = True) -> None:
    """Send a WhatsApp message via OpenWA, splitting into chunks if needed.

    session_id  – OpenWA session name (e.g. "anatummy-wa")
    to_jid      – WhatsApp JID of the recipient (e.g. "923328085405@c.us")
    body        – message text
    """
    import httpx

    base_url = os.environ.get("OPENWA_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("OPENWA_API_KEY", "")
    if not base_url or not api_key:
        logger.warning("openwa_send: OPENWA_BASE_URL or OPENWA_API_KEY not set — skipping send to %s", to_jid)
        return

    url = f"{base_url}/api/sessions/{session_id}/messages/send-text"
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

    chunks = _chunk(body)
    labeled = [
        f"[{i + 1}/{len(chunks)}]\n{c}" if len(chunks) > 1 else c
        for i, c in enumerate(chunks)
    ]

    for i, chunk in enumerate(labeled):
        payload = {"chatId": to_jid, "text": chunk}
        try:
            with httpx.Client(timeout=15) as client:
                r = client.post(url, headers=headers, json=payload)
            logger.info(
                "openwa_send: sent chunk %d/%d status=%d to=%s",
                i + 1, len(chunks), r.status_code, to_jid,
            )
            if r.status_code >= 400:
                logger.error("openwa_send: API error %d body=%s", r.status_code, r.text[:200])
        except Exception as exc:
            logger.error("openwa_send: chunk %d/%d failed: %s", i + 1, len(chunks), exc)
            if retry:
                try:
                    time.sleep(2)
                    with httpx.Client(timeout=15) as client:
                        client.post(url, headers=headers, json=payload)
                    logger.info("openwa_send: retry succeeded chunk %d/%d", i + 1, len(chunks))
                except Exception as exc2:
                    logger.error("openwa_send: retry also failed chunk %d/%d: %s", i + 1, len(chunks), exc2)
        if i < len(chunks) - 1:
            time.sleep(0.3)
