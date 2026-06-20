"""Send a WhatsApp message via Twilio.

Credentials and numbers are read from environment variables so nothing secret
lives in the repo:

  TWILIO_ACCOUNT_SID    - Twilio account SID
  TWILIO_AUTH_TOKEN     - Twilio auth token
  TWILIO_WHATSAPP_FROM  - sender, e.g. "whatsapp:+14155238886" (Twilio sandbox)
  OWNER_WHATSAPP_TO     - recipient, e.g. "whatsapp:+923001234567"

The `twilio` package is imported lazily so the rest of the app (and the test
suite) works without it installed.
"""

from __future__ import annotations

import os


def _ensure_whatsapp_prefix(number: str) -> str:
    return number if number.startswith("whatsapp:") else f"whatsapp:{number}"


def send_whatsapp(
    body: str,
    to: str | None = None,
    from_: str | None = None,
    account_sid: str | None = None,
    auth_token: str | None = None,
) -> str:
    """Send `body` to a WhatsApp number via Twilio and return the message SID.

    Falls back to environment variables for any argument left as None. Raises
    RuntimeError if configuration is incomplete so callers can degrade to a
    dry-run print.
    """
    account_sid = account_sid or os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = auth_token or os.environ.get("TWILIO_AUTH_TOKEN")
    from_ = from_ or os.environ.get("TWILIO_WHATSAPP_FROM")
    to = to or os.environ.get("OWNER_WHATSAPP_TO")

    missing = [
        name for name, val in [
            ("TWILIO_ACCOUNT_SID", account_sid),
            ("TWILIO_AUTH_TOKEN", auth_token),
            ("TWILIO_WHATSAPP_FROM", from_),
            ("OWNER_WHATSAPP_TO", to),
        ] if not val
    ]
    if missing:
        raise RuntimeError(f"Missing Twilio config: {', '.join(missing)}")

    from twilio.rest import Client  # lazy import — only needed when actually sending

    client = Client(account_sid, auth_token)
    message = client.messages.create(
        body=body,
        from_=_ensure_whatsapp_prefix(from_),
        to=_ensure_whatsapp_prefix(to),
    )
    return message.sid
