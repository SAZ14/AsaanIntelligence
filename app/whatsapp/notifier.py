"""Format and send WhatsApp review alerts to the venue owner via Twilio.

Message anatomy (what the owner sees):
  - header: source + rating + sentiment + issue class
  - the review text
  - the reconstructed "true story" from deterministic correlation
  - the drafted reply (from the LLM half)
  - an action prompt: POST / EDIT / IGNORE

DRY_RUN=1 prints the payload instead of sending, so this is runnable
without Twilio credentials (or even the twilio package installed).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.config import env_flag

if TYPE_CHECKING:  # avoid hard import cycles / runtime cost
    from app.agents.reputation import VisitContext
    from app.models.canonical import Review


@dataclass
class SendResult:
    """Outcome of send_review_alert."""

    sent: bool          # True if handed to Twilio, False if dry-run
    to: str
    body: str
    sid: str = ""       # Twilio message SID when actually sent
    dry_run: bool = False


# ── Formatting ──

def _normalize_whatsapp(number: str) -> str:
    """Ensure a number carries the `whatsapp:` channel prefix Twilio expects."""
    number = number.strip()
    if not number:
        return number
    return number if number.startswith("whatsapp:") else f"whatsapp:{number}"


def _build_true_story(correlation: "VisitContext") -> list[str]:
    """Turn the correlation context into plain-language lines."""
    lines: list[str] = []
    conf = getattr(correlation, "confidence", "none")
    if conf == "none":
        lines.append("No reliable visit match — correlation confidence: none.")
        return lines

    when = []
    if correlation.estimated_date:
        when.append(correlation.estimated_date)
    if correlation.estimated_hour_range:
        when.append(correlation.estimated_hour_range)
    if when:
        lines.append("Likely visit: " + " ".join(when))

    if correlation.order_count_in_window > 0:
        busy = " (busy period)" if correlation.order_count_in_window >= 5 else ""
        lines.append(f"{correlation.order_count_in_window} orders in that window{busy}")

    if correlation.matched_staff_name:
        lines.append(f"Staff involved: {correlation.matched_staff_name}")
    elif correlation.staff_on_duty:
        lines.append(f"On duty: {', '.join(correlation.staff_on_duty)}")

    if correlation.match_reasons:
        lines.append("Signals: " + "; ".join(correlation.match_reasons))

    lines.append(f"(correlation confidence: {conf})")
    return lines


def format_review_alert(
    review: "Review",
    correlation: "VisitContext",
    issue: str,
    draft: str,
    sentiment: str = "",
) -> str:
    """Build the WhatsApp message body for one review needing attention."""
    sentiment_part = f" · {sentiment}" if sentiment else ""
    header = f"{review.source} · {review.rating}/5{sentiment_part} · {issue.replace('_', ' ')}"

    story = "\n".join(f"• {ln}" for ln in _build_true_story(correlation))

    parts = [
        "🔔 *Review needs attention*",
        "",
        header,
        f"👤 {review.reviewer_name}",
        f"“{review.text}”",
        "",
        "🔎 *What likely happened*",
        story,
        "",
        "✍️ *Suggested reply*",
        draft.strip() if draft.strip() else "(no draft generated)",
        "",
        "Reply *POST* to publish, *EDIT <your text>* to revise, or *IGNORE* to skip.",
    ]
    return "\n".join(parts)


# ── Sending ──

def _twilio_client(account_sid: str, auth_token: str):
    """Construct a Twilio REST client. Imported lazily so DRY_RUN and tests
    do not require the twilio package or real credentials."""
    from twilio.rest import Client  # noqa: PLC0415 — intentional lazy import

    return Client(account_sid, auth_token)


def send_review_alert(
    owner_number: str,
    review: "Review",
    correlation: "VisitContext",
    issue: str,
    draft: str,
    sentiment: str = "",
    *,
    client=None,
) -> SendResult:
    """Format and deliver a review alert to ``owner_number`` over WhatsApp.

    If DRY_RUN is truthy, the payload is printed and nothing is sent.
    A ``client`` may be injected (used by tests to mock Twilio).
    """
    body = format_review_alert(review, correlation, issue, draft, sentiment)
    to = _normalize_whatsapp(owner_number)

    if env_flag("DRY_RUN"):
        print("─" * 60)
        print(f"[DRY_RUN] would send WhatsApp to {to}:")
        print(body)
        print("─" * 60)
        return SendResult(sent=False, to=to, body=body, dry_run=True)

    from_number = _normalize_whatsapp(os.environ.get("TWILIO_WHATSAPP_NUMBER", ""))
    if client is None:
        account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        if not (account_sid and auth_token and from_number):
            raise RuntimeError(
                "Twilio credentials missing. Set TWILIO_ACCOUNT_SID, "
                "TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_NUMBER, or set DRY_RUN=1."
            )
        client = _twilio_client(account_sid, auth_token)

    msg = client.messages.create(from_=from_number, to=to, body=body)
    return SendResult(sent=True, to=to, body=body, sid=getattr(msg, "sid", ""))
