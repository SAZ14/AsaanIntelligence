"""Send WhatsApp messages via Meta's Cloud API (WhatsApp Business Platform).

Third provider alongside twilio_send.py and openwa_send.py — same interface
shape so callers swap providers by swapping the send function.

Unlike the other two, credentials are per-store: each restaurant onboards
its own WhatsApp Business number to our Meta app via embedded signup, which
yields a phone_number_id + a business access token stored in
store_meta_numbers. The token is looked up at send time so token rotation
takes effect immediately (including for queued jobs resumed after restart).

Env vars:
  META_GRAPH_VERSION – Graph API version (default v23.0)
  META_ACCESS_TOKEN  – optional fallback token when a store has none stored
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1500


def _chunk(text: str, size: int = CHUNK_SIZE) -> list[str]:
    from app.core.twilio_send import _chunk as _tc
    return _tc(text, size)


def _graph_base() -> str:
    version = os.environ.get("META_GRAPH_VERSION", "v23.0")
    return f"https://graph.facebook.com/{version}"


def _token_for(phone_number_id: str) -> str:
    from app.core.db import get_meta_access_token
    token = get_meta_access_token(phone_number_id)
    return token or os.environ.get("META_ACCESS_TOKEN", "")


def send_meta(phone_number_id: str, to_wa_id: str, body: str) -> None:
    """Send a WhatsApp text message via the Cloud API, chunking long texts.

    phone_number_id – the store's Cloud API phone number id (sender)
    to_wa_id        – recipient wa_id, digits only (e.g. "923001234567")
    body            – message text
    """
    import httpx

    token = _token_for(phone_number_id)
    if not token:
        logger.warning("meta_send: no access token for phone_number_id=%s — skipping send", phone_number_id)
        return

    url = f"{_graph_base()}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    chunks = _chunk(body)
    labeled = [
        f"[{i + 1}/{len(chunks)}]\n{c}" if len(chunks) > 1 else c
        for i, c in enumerate(chunks)
    ]
    for i, chunk in enumerate(labeled):
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_wa_id,
            "type": "text",
            "text": {"preview_url": False, "body": chunk},
        }
        try:
            with httpx.Client(timeout=15) as client:
                r = client.post(url, headers=headers, json=payload)
            logger.info(
                "meta_send: sent chunk %d/%d status=%d to=%s from_pnid=%s",
                i + 1, len(chunks), r.status_code, to_wa_id, phone_number_id,
            )
            if r.status_code >= 400:
                logger.error("meta_send: API error %d body=%s", r.status_code, r.text[:300])
        except Exception as exc:
            logger.error("meta_send: chunk %d/%d failed: %s", i + 1, len(chunks), exc)


def download_media(media_id: str, phone_number_id: str) -> tuple[bytes, str, str] | None:
    """Download inbound media by id. Returns (content, mime_type, filename)
    or None. Cloud API media is a two-step fetch: GET /{media_id} returns a
    short-lived URL, which is then fetched with the same bearer token."""
    import httpx

    token = _token_for(phone_number_id)
    if not token:
        logger.warning("meta_send: no token to download media %s", media_id)
        return None
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=30) as client:
            meta = client.get(f"{_graph_base()}/{media_id}", headers=headers)
            meta.raise_for_status()
            info = meta.json()
            url = info.get("url", "")
            if not url:
                logger.error("meta_send: media %s has no url: %s", media_id, str(info)[:200])
                return None
            blob = client.get(url, headers=headers, follow_redirects=True)
            blob.raise_for_status()
        return blob.content, info.get("mime_type", ""), info.get("filename", "") or ""
    except Exception as exc:
        logger.error("meta_send: media download failed id=%s: %s", media_id, exc)
        return None
