"""Send review alerts to the venue owner over the WhatsApp Cloud API.

`send_review_alert` formats an INTERACTIVE message (alert line + reconstructed
"true story" + drafted reply + three reply buttons) and POSTs it to Meta.
`send_text` sends a plain text message.

DRY_RUN (env flag, default on) prints the exact JSON payload instead of calling
Meta, so message formatting can be verified offline and CI never sends anything.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from .config import WhatsAppConfig

# Button reply IDs — the webhook routes on these. Keep in sync with webhook.py.
BTN_POST_REPLY = "reputation:post_reply"
BTN_EDIT = "reputation:edit"
BTN_IGNORE = "reputation:ignore"

# WhatsApp interactive-message limits.
_BODY_MAX = 1024
_BUTTON_TITLE_MAX = 20


def _rating_marker(rating: int) -> str:
    """A glanceable severity marker for the alert line."""
    if rating <= 2:
        return "🔴"
    if rating == 3:
        return "🟡"
    return "🟢"


def _true_story(correlation: Any) -> str:
    """Reconstruct the visit ("true story") from the correlation context.

    `correlation` is duck-typed (VisitContext from the agent), so the notifier
    stays decoupled from the agent's models.
    """
    confidence = getattr(correlation, "confidence", "none")
    if confidence == "none":
        return "Couldn't tie this to a specific visit — no time/staff/item clues in the text."

    bits: list[str] = []
    date = getattr(correlation, "estimated_date", "")
    hours = getattr(correlation, "estimated_hour_range", "")
    if date:
        when = date + (f", {hours}" if hours else "")
        bits.append(f"Likely visit: {when}")
    load = getattr(correlation, "order_count_in_window", 0)
    if load:
        bits.append(f"{load} orders in that window (busy)")
    staff_name = getattr(correlation, "matched_staff_name", "")
    if staff_name:
        bits.append(f"staff on the floor: {staff_name}")
    reasons = getattr(correlation, "match_reasons", None) or []
    if reasons:
        bits.append("clues: " + ", ".join(reasons))

    return f"({confidence} confidence) " + "; ".join(bits) if bits else f"({confidence} confidence)"


def format_alert_text(review: Any, correlation: Any, issue: str, draft: str) -> str:
    """Build the message body: alert line, true story, and drafted reply."""
    rating = getattr(review, "rating", 0)
    source = getattr(review, "source", "unknown")
    reviewer = getattr(review, "reviewer_name", "") or "Anonymous"

    alert = f"{_rating_marker(rating)} New {rating}/5 review on {source} — {reviewer} ({issue})"
    story = _true_story(correlation)

    body = (
        f"{alert}\n\n"
        f"*What happened*\n{story}\n\n"
        f"*Drafted reply*\n{draft}"
    )
    if len(body) > _BODY_MAX:
        body = body[: _BODY_MAX - 1].rstrip() + "…"
    return body


def _button(reply_id: str, title: str) -> dict[str, Any]:
    return {"type": "reply", "reply": {"id": reply_id, "title": title[:_BUTTON_TITLE_MAX]}}


def build_review_alert_payload(
    owner_number: str,
    review: Any,
    correlation: Any,
    issue: str,
    draft: str,
) -> dict[str, Any]:
    """Build the exact Cloud API request body for an interactive review alert.

    Pure (no I/O) so it's trivially unit-testable.
    """
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": owner_number,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": format_alert_text(review, correlation, issue, draft)},
            "action": {
                "buttons": [
                    _button(BTN_POST_REPLY, "Post reply"),
                    _button(BTN_EDIT, "Edit"),
                    _button(BTN_IGNORE, "Ignore"),
                ]
            },
        },
    }


def build_text_payload(to: str, text: str) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }


def _dispatch(payload: dict[str, Any], config: WhatsAppConfig) -> dict[str, Any]:
    """POST the payload to Meta, or print it under DRY_RUN."""
    if config.dry_run:
        print("── WhatsApp DRY_RUN — would POST to", config.messages_url)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return {"dry_run": True, "url": config.messages_url, "payload": payload}

    config.require_send_credentials()
    resp = httpx.post(
        config.messages_url,
        headers={
            "Authorization": f"Bearer {config.token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def send_review_alert(
    owner_number: str,
    review: Any,
    correlation: Any,
    issue: str,
    draft: str,
    config: WhatsAppConfig | None = None,
) -> dict[str, Any]:
    """Format and send the interactive review alert to the owner."""
    config = config or WhatsAppConfig.from_env()
    payload = build_review_alert_payload(owner_number, review, correlation, issue, draft)
    return _dispatch(payload, config)


def send_text(
    to: str,
    text: str,
    config: WhatsAppConfig | None = None,
) -> dict[str, Any]:
    """Send a plain text WhatsApp message."""
    config = config or WhatsAppConfig.from_env()
    return _dispatch(build_text_payload(to, text), config)
