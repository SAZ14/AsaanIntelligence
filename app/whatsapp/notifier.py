"""Thin Twilio WhatsApp notifier, shared across agents.

There was no WhatsApp/Twilio delivery layer in the repo when the Customer
agent was built, so this module is new and intentionally generic — the
Reputation agent (or any other) can import the same ``WhatsAppNotifier``.

Safety model:
- ``DRY_RUN`` defaults to ON. In dry-run the notifier records messages instead
  of sending them, so CI and the mock feed never hit the network and Twilio is
  not even imported (it is a lazy import inside the real send path). This keeps
  tests and `scripts/customer_live.py` green without credentials.
- To send for real, set ``WHATSAPP_DRY_RUN=0`` and provide Twilio credentials
  via env (``TWILIO_ACCOUNT_SID``, ``TWILIO_AUTH_TOKEN``, ``WHATSAPP_FROM``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_truthy(val: str) -> bool:
    return val.strip().lower() not in ("0", "false", "no", "off", "")


def _normalize(number: str) -> str:
    """Ensure a WhatsApp-prefixed E.164 recipient (Twilio convention)."""
    number = number.strip()
    if not number:
        return number
    if not number.startswith("whatsapp:"):
        number = f"whatsapp:{number}"
    return number


@dataclass
class SentMessage:
    to: str
    body: str
    status: str
    sid: str = ""


@dataclass
class WhatsAppNotifier:
    """Send (or, in dry-run, record) WhatsApp messages via Twilio."""

    dry_run: bool | None = None
    from_number: str = ""
    account_sid: str = ""
    auth_token: str = ""
    sent: list[SentMessage] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.dry_run is None:
            self.dry_run = _env_truthy(os.environ.get("WHATSAPP_DRY_RUN", "1"))
        self.from_number = self.from_number or os.environ.get(
            "WHATSAPP_FROM", "whatsapp:+14155238886"
        )
        self.account_sid = self.account_sid or os.environ.get("TWILIO_ACCOUNT_SID", "")
        self.auth_token = self.auth_token or os.environ.get("TWILIO_AUTH_TOKEN", "")

    def send(self, to: str, body: str) -> SentMessage:
        to = _normalize(to)
        if self.dry_run:
            msg = SentMessage(to=to, body=body, status="dry_run")
            self.sent.append(msg)
            return msg

        # Real send — lazy import so Twilio is never a hard/CI dependency.
        from twilio.rest import Client  # type: ignore

        client = Client(self.account_sid, self.auth_token)
        resp = client.messages.create(
            from_=_normalize(self.from_number), to=to, body=body
        )
        msg = SentMessage(to=to, body=body, status=resp.status, sid=resp.sid or "")
        self.sent.append(msg)
        return msg
