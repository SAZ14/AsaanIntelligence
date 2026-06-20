#!/usr/bin/env python3
"""Twilio WhatsApp connectivity test.

Sends a single fixed message from TWILIO_WHATSAPP_NUMBER to OWNER_NUMBER to
verify credentials, egress, and sandbox opt-in. Honors DRY_RUN. On failure
prints the categorized Twilio error.

Usage:  python scripts/send_whatsapp_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.notify import ConfigError, WhatsAppNotifier, WhatsAppSendError, WhatsAppSettings

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    try:
        settings = WhatsAppSettings.from_env(dotenv_path=ROOT / ".env")
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        return 2

    body = "AsaanPay Enterprise — Twilio WhatsApp connectivity test ✅"
    print(f"from={settings.sender}  to={settings.recipient}  "
          f"dry_run={settings.dry_run}  sandbox={settings.is_sandbox}")

    notifier = WhatsAppNotifier(settings)
    try:
        result = notifier.send_and_confirm(body)
    except WhatsAppSendError as e:
        print(f"SEND FAILED [{e.category.value}] code={e.code}")
        print(f"  {e}")
        return 1

    if result.dry_run:
        print("[DRY_RUN] not sent. Body:", body)
    else:
        print(f"DELIVERED: sid={result.sid} status={result.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
