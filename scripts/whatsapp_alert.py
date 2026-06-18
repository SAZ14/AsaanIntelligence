#!/usr/bin/env python3
"""Push a proactive WhatsApp audit alert to a venue's owner.

Computes the venue's audit and sends the headline + leakage summary outbound via
Twilio. Intended to run on a schedule (cron) for a daily/weekly digest, or
ad-hoc. Requires TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM.

    python scripts/whatsapp_alert.py roastery --to whatsapp:+923001234567
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.venues import OWNER_WHATSAPP, RESTAURANTS
from app.whatsapp.service import IntegrityWhatsAppService
from app.whatsapp.twilio_client import TwilioNotConfigured, send_whatsapp


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a proactive WhatsApp audit alert.")
    parser.add_argument("venue", choices=sorted(RESTAURANTS))
    parser.add_argument("--to", help="owner WhatsApp number (defaults to registry)")
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = parser.parse_args()

    to = args.to
    if not to:
        to = next((num for num, v in OWNER_WHATSAPP.items() if v == args.venue), None)
    if not to and not args.dry_run:
        sys.exit(f"No owner number for {args.venue}; pass --to or set OWNER_WHATSAPP.")

    svc = IntegrityWhatsAppService()
    report = svc.get_report(args.venue, force=True)
    message = svc._fmt_summary(report) + "\n\n" + svc._fmt_leakage(report)

    if args.dry_run:
        print(f"[dry-run] would send to {to or '<owner>'}:\n\n{message}")
        return

    try:
        sid = send_whatsapp(to, message)
        print(f"Sent ({sid}) to {to}")
    except TwilioNotConfigured as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
