#!/usr/bin/env python3
"""Scheduled WhatsApp digests for every registered owner.

Two cadences, designed to run from cron:

  • **Daily** — yesterday's report (sales, profit, leakage vs the day before).

        # 8am every day
        0 8 * * *  cd /path/to/repo && python scripts/digest_worker.py --kind daily

  • **Weekly** — the week summarised (7-day totals, trend, top issues) vs the
    previous week.

        # 8am every Monday
        0 8 * * 1  cd /path/to/repo && python scripts/digest_worker.py --kind weekly

  • **Full** — the all-time audit summary + leakage (the original digest).

A built-in loop is also available for environments without cron::

        python scripts/digest_worker.py --kind daily --every-min 1440

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


def _build_message(svc: IntegrityWhatsAppService, venue_key: str, kind: str) -> str:
    if kind == "daily":
        return svc.daily_digest(venue_key, force=True)
    if kind == "weekly":
        return svc.weekly_digest(venue_key, force=True)
    # "full": the all-time audit summary + leakage breakdown.
    report = svc.get_report(venue_key, force=True)
    return svc._fmt_summary(report) + "\n\n" + svc._fmt_leakage(report)


def send_digests(kind: str = "daily", venue: str | None = None, dry_run: bool = False) -> int:
    svc = IntegrityWhatsAppService()
    recipients = {num: v for num, v in OWNER_WHATSAPP.items() if venue is None or v == venue}
    if not recipients:
        print(f"[{datetime.now():%Y-%m-%d %H:%M}] no owners registered"
              f"{f' for {venue}' if venue else ''} — nothing to send.")
        return 0

    sent = 0
    for number, venue_key in recipients.items():
        try:
            message = _build_message(svc, venue_key, kind)
        except Exception as e:  # one bad venue shouldn't stop the rest
            print(f"  ! {venue_key}: failed to build {kind} digest ({e})")
            continue

        if dry_run:
            print(f"[dry-run] {kind} -> {number} ({venue_key}):\n{message}\n{'-' * 50}")
            sent += 1
            continue
        try:
            sid = send_whatsapp(number, message)
            print(f"  ✓ {kind} {venue_key} -> {number} ({sid})")
            sent += 1
        except TwilioNotConfigured as e:
            sys.exit(str(e))
        except Exception as e:
            print(f"  ! {venue_key} -> {number}: send failed ({e})")
    return sent


def main() -> None:
    parser = argparse.ArgumentParser(description="Send scheduled WhatsApp digests.")
    parser.add_argument("--kind", choices=("daily", "weekly", "full"), default="daily",
                        help="which digest to send (default: daily)")
    parser.add_argument("--once", action="store_true", help="send a single round and exit (cron)")
    parser.add_argument("--every-min", type=int, help="loop, sending every N minutes")
    parser.add_argument("--venue", help="restrict to one venue key")
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = parser.parse_args()

    if args.every_min:
        print(f"Digest worker started — {args.kind} every {args.every_min} min. Ctrl-C to stop.")
        while True:
            n = send_digests(args.kind, args.venue, args.dry_run)
            print(f"[{datetime.now():%Y-%m-%d %H:%M}] sent {n} {args.kind} digest(s).")
            time.sleep(args.every_min * 60)
    else:
        n = send_digests(args.kind, args.venue, args.dry_run)
        print(f"Sent {n} {args.kind} digest(s).")


if __name__ == "__main__":
    main()
