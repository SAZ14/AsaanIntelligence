"""Twilio SMS notifier — for PROACTIVE messages that must avoid Meta.

The loyalty stamp replies go over WhatsApp (no Meta setup needed, because the
customer messages first). But proactive "we miss you" / event-invite messages
are business-initiated and days later — on WhatsApp those would require Meta
business verification + approved templates. SMS has no such requirement, so we
send those over SMS instead: you already have the customer's number from the
scan.

Safety model mirrors WhatsAppNotifier:
- ``DRY_RUN`` defaults ON (env ``SMS_DRY_RUN``); in dry-run messages are
  recorded, not sent, and Twilio is not imported.
- To send for real, set ``SMS_DRY_RUN=0`` and provide ``SMS_FROM`` (a Twilio
  SMS-capable number) plus ``TWILIO_ACCOUNT_SID`` / ``TWILIO_AUTH_TOKEN``.

An opt-out line ("Reply STOP to opt out.") is appended automatically — good
practice and required for marketing SMS in most regions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from app.whatsapp.notifier import SentMessage, _env_truthy

DEFAULT_OPT_OUT = "Reply STOP to opt out."


def _to_e164(number: str) -> str:
    """Plain phone number for SMS (no whatsapp: prefix)."""
    return number.replace("whatsapp:", "").strip()


@dataclass
class SmsNotifier:
    """Send (or, in dry-run, record) SMS messages via Twilio."""

    dry_run: bool | None = None
    from_number: str = ""
    account_sid: str = ""
    auth_token: str = ""
    opt_out_notice: str = DEFAULT_OPT_OUT
    sent: list[SentMessage] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.dry_run is None:
            self.dry_run = _env_truthy(os.environ.get("SMS_DRY_RUN", "1"))
        self.from_number = self.from_number or os.environ.get("SMS_FROM", "+10000000000")
        self.account_sid = self.account_sid or os.environ.get("TWILIO_ACCOUNT_SID", "")
        self.auth_token = self.auth_token or os.environ.get("TWILIO_AUTH_TOKEN", "")

    def _with_opt_out(self, body: str) -> str:
        if self.opt_out_notice and "STOP" not in body.upper():
            return f"{body}\n\n{self.opt_out_notice}"
        return body

    def send(self, to: str, body: str) -> SentMessage:
        to = _to_e164(to)
        body = self._with_opt_out(body)
        if self.dry_run:
            msg = SentMessage(to=to, body=body, status="dry_run")
            self.sent.append(msg)
            return msg

        # Real send — lazy import so Twilio is never a hard/CI dependency.
        from twilio.rest import Client  # type: ignore

        client = Client(self.account_sid, self.auth_token)
        resp = client.messages.create(from_=_to_e164(self.from_number), to=to, body=body)
        msg = SentMessage(to=to, body=body, status=resp.status, sid=resp.sid or "")
        self.sent.append(msg)
        return msg
