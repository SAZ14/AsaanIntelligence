"""Twilio WhatsApp messaging helpers."""

from __future__ import annotations

import os
import re


def normalize_phone(phone: str) -> str:
    p = re.sub(r"[\s\-()]", "", phone.strip())
    if p.startswith("00"):
        p = "+" + p[2:]
    if p.startswith("0") and len(p) == 11:
        p = "+92" + p[1:]
    if p.startswith("92") and not p.startswith("+"):
        p = "+" + p
    if not p.startswith("+"):
        p = "+" + p
    return p


def whatsapp_address(phone: str) -> str:
    normalized = normalize_phone(phone)
    if normalized.startswith("whatsapp:"):
        return normalized
    return f"whatsapp:{normalized}"


def parse_twilio_whatsapp_phone(raw: str) -> str:
    value = raw.strip()
    if value.lower().startswith("whatsapp:"):
        value = value.split(":", 1)[1]
    return normalize_phone(value)


def twilio_whatsapp_digits(env_key: str = "TWILIO_WHATSAPP_CUSTOMER_FROM") -> str:
    raw = os.environ.get(env_key, "")
    if not raw:
        return ""
    return parse_twilio_whatsapp_phone(raw).lstrip("+")


def send_whatsapp_text(
    to_phone: str,
    body: str,
    *,
    from_key: str = "TWILIO_WHATSAPP_CUSTOMER_FROM",
    use_twilio: bool | None = None,
) -> str:
    if use_twilio is None:
        use_twilio = bool(
            os.environ.get("TWILIO_ACCOUNT_SID") and os.environ.get("TWILIO_AUTH_TOKEN"),
        )
    to_addr = whatsapp_address(parse_twilio_whatsapp_phone(to_phone))
    if not use_twilio:
        print(f"[WHATSAPP] → {to_addr}\n  {body}\n")
        return ""
    from twilio.rest import Client

    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token = os.environ["TWILIO_AUTH_TOKEN"]
    from_number = os.environ.get(from_key, "whatsapp:+14155238886")
    if not from_number.startswith("whatsapp:"):
        from_number = f"whatsapp:{from_number}"
    client = Client(account_sid, auth_token)
    from app.core.twilio_send import send_whatsapp
    send_whatsapp(to=to_addr, body=body, from_=from_number)
    return ""


async def send_whatsapp_typing_indicator(message_sid: str) -> None:
    """Send a typing indicator for an incoming WhatsApp message asynchronously."""
    if not message_sid:
        return
    import os
    import httpx
    import asyncio
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                'https://messaging.twilio.com/v3/Indicators/Typing.json',
                auth=(account_sid, auth_token),
                json={'channel': 'WHATSAPP', 'messageId': message_sid},
                timeout=3.0
            )
    except Exception:
        pass

