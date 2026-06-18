#!/usr/bin/env python3
"""Scheduled WhatsApp audit digests for every registered owner.

Sends each owner in ``OWNER_WHATSAPP`` a fresh summary + leakage digest for their
venue. Designed for a scheduler:

  • Cron (recommended) — one run per day, e.g. 8am::

        0 8 * * *  cd /path/to/repo && python scripts/digest_worker.py --once

  • Built-in loop — keep a worker alive and fire every N minutes::

        python scripts/digest_worker.py --every-min 1440

Sending needs TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM.
Use --dry-run to preview without sending (and without credentials).
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.venues import OWNER_WHATSAPP
from app.whatsapp.service import IntegrityWhatsAppService
from app.whatsapp.twilio_client import TwilioNotConfigured, send_whatsapp


def _digest(svc: IntegrityWhatsAppService, venue_key: str) -> str:
    report = svc.get_report(venue_key, force=True)
    return svc._fmt_summary(report) + "\n\n" + svc._fmt_leakage(report)


def send_digests(venue: str | None = None, dry_run: bool = False) -> int:
    svc = IntegrityWhatsAppService()
    recipients = {num: v for num, v in OWNER_WHATSAPP.items() if venue is None or v == venue}
    if not recipients:
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] no owners registered"
              f"{f' for {venue}' if venue else ''} — nothing to send.")
        return 0

    sent = 0
    for number, venue_key in recipients.items():
        try:
            message = _digest(svc, venue_key)
        except Exception as e:  # one bad venue shouldn't stop the rest
            print(f"  ! {venue_key}: failed to build digest ({e})")
            continue

        if dry_run:
            print(f"[dry-run] -> {number} ({venue_key}):\n{message}\n{'-' * 50}")
            sent += 1
            continue
        try:
            sid = send_whatsapp(number, message)
            print(f"  ✓ {venue_key} -> {number} ({sid})")
            sent += 1
        except TwilioNotConfigured as e:
            sys.exit(str(e))
        except Exception as e:
            print(f"  ! {venue_key} -> {number}: send failed ({e})")
    return sent


def main() -> None:
    parser = argparse.ArgumentParser(description="Send scheduled WhatsApp audit digests.")
    parser.add_argument("--once", action="store_true", help="send a single round and exit (cron)")
    parser.add_argument("--every-min", type=int, help="loop, sending every N minutes")
    parser.add_argument("--venue", help="restrict to one venue key")
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = parser.parse_args()

    if args.every_min:
        print(f"Digest worker started — every {args.every_min} min. Ctrl-C to stop.")
        while True:
            n = send_digests(args.venue, args.dry_run)
            print(f"[{datetime.now():%Y-%m-%d %H:%M}] sent {n} digest(s).")
            time.sleep(args.every_min * 60)
    else:
        n = send_digests(args.venue, args.dry_run)
        print(f"Sent {n} digest(s).")


if __name__ == "__main__":
    main()
