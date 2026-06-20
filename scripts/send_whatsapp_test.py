"""Live Twilio WhatsApp send test.

Reads credentials/numbers from the environment (or a local .env), then sends a
single WhatsApp message from TWILIO_WHATSAPP_NUMBER to OWNER_NUMBER.

Honors DRY_RUN: when DRY_RUN is anything other than "0"/"false"/"" the message
is NOT sent and the intended payload is printed instead.

Usage:  python scripts/send_whatsapp_test.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (no external dependency). Does not override real env."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def first_env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    sid = first_env("TWILIO_ACCOUNT_SID")
    token = first_env("TWILIO_AUTH_TOKEN")
    sender = first_env("TWILIO_WHATSAPP_NUMBER", "TWILIO_WHATSAPP_FROM")
    recipient = first_env("OWNER_NUMBER", "TWILIO_WHATSAPP_TO")
    dry_run = (os.environ.get("DRY_RUN", "0").strip().lower() not in ("0", "false", ""))

    missing = [
        name
        for name, val in (
            ("TWILIO_ACCOUNT_SID", sid),
            ("TWILIO_AUTH_TOKEN", token),
            ("TWILIO_WHATSAPP_NUMBER/FROM", sender),
            ("OWNER_NUMBER/TWILIO_WHATSAPP_TO", recipient),
        )
        if not val
    ]
    if missing:
        print("MISSING required values:", ", ".join(missing), file=sys.stderr)
        return 2

    body = "AsaanPay Enterprise — Twilio WhatsApp live send test ✅"
    print(f"from={sender}  to={recipient}  dry_run={dry_run}")

    if dry_run:
        print("[DRY_RUN] not sending. Body:", body)
        return 0

    from twilio.base.exceptions import TwilioRestException
    from twilio.rest import Client

    client = Client(sid, token)
    try:
        msg = client.messages.create(from_=sender, to=recipient, body=body)
    except TwilioRestException as e:
        print("SEND FAILED (TwilioRestException)")
        print("  http_status:", e.status)
        print("  code:", e.code)
        print("  message:", e.msg)
        print("  more_info:", getattr(e, "uri", None) or getattr(e, "details", None))
        return 1
    except Exception as e:  # noqa: BLE001
        print("SEND FAILED (non-Twilio):", type(e).__name__, str(e))
        return 1

    print("SEND ACCEPTED by Twilio API")
    print("  message_sid:", msg.sid)
    print("  status:", msg.status)
    print("  error_code:", msg.error_code)
    print("  error_message:", msg.error_message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
