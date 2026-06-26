"""Twilio WhatsApp channel for the Revenue agent: inbound webhook parsing +
outbound sending (for scheduled digests).

Degrades gracefully — with no Twilio credentials it logs instead of sending, so
the agent runs end-to-end in development and tests.
"""

from __future__ import annotations

import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from xml.sax.saxutils import escape


@dataclass
class InboundMessage:
    from_phone: str          # the owner who texted
    to_number: str           # the café's WhatsApp contact that was messaged
    body: str
    profile_name: str = ""
    raw_from: str = ""


def _strip_wa(value: str) -> str:
    return value[len("whatsapp:"):] if value.startswith("whatsapp:") else value


def parse_inbound(form: dict) -> InboundMessage:
    raw_from = form.get("From", "") or ""
    return InboundMessage(
        from_phone=_strip_wa(raw_from).strip(),
        to_number=_strip_wa(form.get("To", "") or "").strip(),
        body=(form.get("Body", "") or "").strip(),
        profile_name=(form.get("ProfileName", "") or "").strip(),
        raw_from=raw_from,
    )


def twiml_reply(text: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f"<Response><Message>{escape(text)}</Message></Response>")


class WhatsAppClient:
    """Outbound sender for proactive messages (digests).

    Env: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM.
    """

    def __init__(self, account_sid=None, auth_token=None, from_number=None) -> None:
        self.account_sid = account_sid or os.environ.get("TWILIO_ACCOUNT_SID", "")
        self.auth_token = auth_token or os.environ.get("TWILIO_AUTH_TOKEN", "")
        self.from_number = from_number or os.environ.get("TWILIO_WHATSAPP_FROM", "")
        self.sent_log: list[tuple[str, str]] = []

    @property
    def configured(self) -> bool:
        return bool(self.account_sid and self.auth_token and self.from_number)

    def send(self, to_phone: str, body: str, from_number: str | None = None) -> bool:
        self.sent_log.append((to_phone, body))
        sender = from_number or self.from_number
        if not (self.account_sid and self.auth_token and sender):
            print(f"[whatsapp:log-only] {sender or '?'} → {to_phone}: {body}")
            return False
        to = to_phone if to_phone.startswith("whatsapp:") else f"whatsapp:{to_phone}"
        frm = sender if sender.startswith("whatsapp:") else f"whatsapp:{sender}"
        url = (f"https://api.twilio.com/2010-04-01/Accounts/"
               f"{self.account_sid}/Messages.json")
        data = urllib.parse.urlencode({"To": to, "From": frm, "Body": body}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, self.account_sid, self.auth_token)
        opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(mgr))
        try:
            with opener.open(req, timeout=10) as resp:
                return 200 <= resp.status < 300
        except Exception as e:
            print(f"[whatsapp:error] failed to send to {to_phone}: {e}")
            return False

