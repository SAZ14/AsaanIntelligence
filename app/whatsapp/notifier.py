from __future__ import annotations

import logging
from typing import Any

from app.whatsapp.config import WhatsAppConfig

logger = logging.getLogger("whatsapp.notifier")


def wa_address(number: str) -> str:
    """Ensure the number has a whatsapp: prefix."""
    if number.startswith("whatsapp:"):
        return number
    return f"whatsapp:{number}"


def format_alert_text(review: dict[str, Any], ai_summary: dict[str, Any]) -> str:
    rating = review.get("rating")
    stars = f"{'⭐' * int(rating)}" if rating else "N/A"
    text = review.get("text", "")
    sentiment = ai_summary.get("sentiment", "")
    draft = ai_summary.get("draft_reply", "")
    corr = ai_summary.get("correlation") or {}

    matched_staff = corr.get("matched_staff_name")
    visit_info = (
        f"\n\n👤 Likely served by: *{matched_staff}*"
        if matched_staff
        else ""
    )

    return (
        f"📝 *New Review* [{review.get('source', 'Unknown')}]\n"
        f"Rating: {stars} ({rating}/5)\n\n"
        f'"{text}"\n\n'
        f"*Sentiment:* {sentiment}"
        f"{visit_info}\n\n"
        f"*Suggested reply:*\n{draft}\n\n"
        "Reply *POST* to publish · *EDIT <text>* to revise · *IGNORE* to skip"
    )


def twilio_alert_body(review: dict, ai_summary: dict) -> str:
    return format_alert_text(review, ai_summary)


def send_text(to_number: str, body: str, config: WhatsAppConfig | None = None) -> bool:
    if config is None:
        config = WhatsAppConfig.from_env()

    if config.dry_run:
        logger.info("[DRY_RUN] to=%s body=%r", to_number, body[:120])
        return True

    if not config.is_valid():
        logger.warning("WhatsApp config incomplete — skipping send to %s", to_number)
        return False

    try:
        from twilio.rest import Client
        client = Client(config.account_sid, config.auth_token)
        msg = client.messages.create(
            from_=wa_address(config.from_number),
            to=wa_address(to_number),
            body=body,
        )
        logger.info("Sent WhatsApp SID=%s to=%s", msg.sid, to_number)
        return True
    except Exception as exc:
        logger.error("Twilio send_text failed: %s", exc)
        return False


def send_review_alert(
    to_number: str,
    review: dict[str, Any],
    ai_summary: dict[str, Any],
    config: WhatsAppConfig | None = None,
) -> bool:
    body = format_alert_text(review, ai_summary)
    return send_text(to_number, body, config=config)
