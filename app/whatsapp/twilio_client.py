"""Outbound WhatsApp sender (Twilio) for proactive alerts.

Inbound questions are answered by replying to Twilio's webhook (no creds needed
— see ``webhook.py``). To *push* a message to the owner unprompted (a daily
summary, a high-severity leakage alert), we call Twilio's REST API, which needs
account credentials.

Uses the official ``twilio`` SDK when installed; otherwise falls back to a
stdlib ``urllib`` POST so the package stays dependency-light. Credentials come
from the environment:

    TWILIO_ACCOUNT_SID
    TWILIO_AUTH_TOKEN
    TWILIO_WHATSAPP_FROM   e.g. "whatsapp:+14155238886"
"""

from __future__ import annotations

import base64
import os
from urllib import parse, request

try:  # optional
    from twilio.rest import Client as _TwilioClient
except Exception:  # pragma: no cover
    _TwilioClient = None  # type: ignore


class TwilioNotConfigured(RuntimeError):
    pass


def _creds() -> tuple[str, str, str]:
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    sender = os.environ.get("TWILIO_WHATSAPP_FROM", "")
    if not (sid and token and sender):
        raise TwilioNotConfigured(
            "Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM."
        )
    return sid, token, sender


def _ensure_whatsapp(number: str) -> str:
    return number if number.startswith("whatsapp:") else f"whatsapp:{number}"


def send_whatsapp(to_number: str, body: str) -> str:
    """Send a WhatsApp message to the owner. Returns the message SID."""
    sid, token, sender = _creds()
    to = _ensure_whatsapp(to_number)

    if _TwilioClient is not None:
        client = _TwilioClient(sid, token)
        msg = client.messages.create(from_=sender, to=to, body=body)
        return msg.sid

    # Dependency-free fallback: raw REST call.
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = parse.urlencode({"From": sender, "To": to, "Body": body}).encode()
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req = request.Request(url, data=data, headers={
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with request.urlopen(req, timeout=30) as resp:  # noqa: S310
        import json
        return json.loads(resp.read().decode()).get("sid", "")
