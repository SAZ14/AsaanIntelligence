"""Twilio WhatsApp channel: inbound webhook parsing + outbound sending.

Inbound Twilio webhooks are ``application/x-www-form-urlencoded`` with fields
like ``From``, ``Body`` and ``ProfileName``. Replies can be returned inline as
TwiML (no credentials needed), while proactive messages (e.g. waitlist offers)
go out via Twilio's REST API.

The outbound client degrades gracefully: with no credentials configured it logs
instead of sending, so the agent runs end-to-end in development and tests.
"""

from __future__ import annotations

import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from xml.sax.saxutils import escape


@dataclass
class InboundMessage:
    from_phone: str          # normalised, e.g. "+923001234567"
    body: str
    profile_name: str = ""
    raw_from: str = ""       # original, e.g. "whatsapp:+923001234567"


def parse_inbound(form: dict) -> InboundMessage:
    """Parse a Twilio inbound-message webhook form into an InboundMessage."""
    raw_from = form.get("From", "") or ""
    phone = raw_from
    if phone.startswith("whatsapp:"):
        phone = phone[len("whatsapp:"):]
    phone = phone.strip()
    return InboundMessage(
        from_phone=phone,
        body=(form.get("Body", "") or "").strip(),
        profile_name=(form.get("ProfileName", "") or "").strip(),
        raw_from=raw_from,
    )


def twiml_reply(text: str) -> str:
    """Build a TwiML response that replies inline to the inbound message."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{escape(text)}</Message></Response>"
    )


class WhatsAppClient:
    """Outbound sender for proactive messages (waitlist offers, reminders).

    Reads credentials from the environment:
        TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM
    If any are missing it runs in log-only mode.
    """

    def __init__(
        self,
        account_sid: str | None = None,
        auth_token: str | None = None,
        from_number: str | None = None,
    ) -> None:
        self.account_sid = account_sid or os.environ.get("TWILIO_ACCOUNT_SID", "")
        self.auth_token = auth_token or os.environ.get("TWILIO_AUTH_TOKEN", "")
        self.from_number = from_number or os.environ.get("TWILIO_WHATSAPP_FROM", "")
        self.sent_log: list[tuple[str, str]] = []

    @property
    def configured(self) -> bool:
        return bool(self.account_sid and self.auth_token and self.from_number)

    def send(self, to_phone: str, body: str) -> bool:
        """Send a WhatsApp message. Returns True if dispatched to Twilio.

        In log-only mode (no creds) it records the message and returns False.
        """
        self.sent_log.append((to_phone, body))
        if not self.configured:
            print(f"[whatsapp:log-only] → {to_phone}: {body}")
            return False

        to = to_phone if to_phone.startswith("whatsapp:") else f"whatsapp:{to_phone}"
        frm = (self.from_number if self.from_number.startswith("whatsapp:")
               else f"whatsapp:{self.from_number}")
        url = (f"https://api.twilio.com/2010-04-01/Accounts/"
               f"{self.account_sid}/Messages.json")
        data = urllib.parse.urlencode({"To": to, "From": frm, "Body": body}).encode()

        req = urllib.request.Request(url, data=data, method="POST")
        token = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        token.add_password(None, url, self.account_sid, self.auth_token)
        auth_handler = urllib.request.HTTPBasicAuthHandler(token)
        opener = urllib.request.build_opener(auth_handler)
        try:
            with opener.open(req, timeout=10) as resp:
                return 200 <= resp.status < 300
        except Exception as e:  # network / auth failure — don't crash the webhook
            print(f"[whatsapp:error] failed to send to {to_phone}: {e}")
            return False
