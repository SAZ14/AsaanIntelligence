from __future__ import annotations

import json
import re
from typing import Any

from .config import WhatsAppConfig

BTN_POST_REPLY = "reputation:post_reply"
BTN_EDIT = "reputation:edit"
BTN_IGNORE = "reputation:ignore"

ACTION_PROMPT = "Reply *POST* to publish · *EDIT* to revise · *IGNORE* to skip."

_BODY_MAX = 1024
_BUTTON_TITLE_MAX = 20


def _rating_marker(rating: int) -> str:
    if rating <= 2:
        return "🔴"
    if rating == 3:
        return "🟡"
    return "🟢"


def _true_story(correlation: Any) -> str:
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


def wa_address(number: str) -> str:
    n = (number or "").strip()
    if n.startswith("whatsapp:"):
        return n
    cleaned = re.sub(r"[\s()\-]", "", n)
    if cleaned and not cleaned.startswith("+") and cleaned.isdigit():
        cleaned = "+" + cleaned
    return f"whatsapp:{cleaned}"


def twilio_alert_body(review: Any, correlation: Any, issue: str, draft: str) -> str:
    body = f"{format_alert_text(review, correlation, issue, draft)}\n\n{ACTION_PROMPT}"
    if len(body) > _BODY_MAX:
        body = body[: _BODY_MAX - 1].rstrip() + "…"
    return body


def build_twilio_params(to: str, body: str, config: WhatsAppConfig) -> dict[str, Any]:
    return {
        "from_": wa_address(config.twilio_whatsapp_number),
        "to": wa_address(to),
        "body": body,
    }


def _send_via_twilio(params: dict[str, Any], config: WhatsAppConfig) -> dict[str, Any]:
    if config.dry_run:
        print("── WhatsApp DRY_RUN — would send via Twilio:")
        print(json.dumps(params, indent=2, ensure_ascii=False))
        return {"dry_run": True, "provider": "twilio", "params": params}

    config.require_twilio_credentials()
    from twilio.rest import Client

    client = Client(config.twilio_account_sid, config.twilio_auth_token)
    msg = client.messages.create(**params)
    return {"provider": "twilio", "sid": msg.sid, "status": msg.status}


def send_review_alert(
    owner_number: str,
    review: Any,
    correlation: Any,
    issue: str,
    draft: str,
    config: WhatsAppConfig | None = None,
) -> dict[str, Any]:
    config = config or WhatsAppConfig.from_env()
    body = twilio_alert_body(review, correlation, issue, draft)
    params = build_twilio_params(owner_number, body, config)
    return _send_via_twilio(params, config)


def send_text(
    to: str,
    text: str,
    config: WhatsAppConfig | None = None,
) -> dict[str, Any]:
    config = config or WhatsAppConfig.from_env()
    return _send_via_twilio(build_twilio_params(to, text, config), config)
