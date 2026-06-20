#!/usr/bin/env python3
"""Send the audit headline summary to the venue owner over WhatsApp.

Runs the analysis pipeline, renders the owner summary, and delivers it via
Twilio. Honors DRY_RUN. On failure prints the categorized error and exits
non-zero.

Usage:
    python scripts/send_report_whatsapp.py [--template CONTENT_SID]

With --template, the summary's key numbers are sent as content variables to an
approved WhatsApp template (needed when outside the 24h session window).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analysis.integrity import analyze_integrity
from app.analysis.retention import analyze_operations, analyze_retention
from app.ingest import load_dataset
from app.notify import (
    ConfigError,
    WhatsAppNotifier,
    WhatsAppSendError,
    WhatsAppSettings,
    owner_summary,
)
from app.report.render import compute_headlines

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", metavar="CONTENT_SID", default=None,
                        help="Send as an approved WhatsApp template instead of freeform text.")
    parser.add_argument("--venue", default=None, help="Venue name for the message header.")
    args = parser.parse_args()

    try:
        settings = WhatsAppSettings.from_env(dotenv_path=ROOT / ".env")
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        return 2

    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )
    integrity = analyze_integrity(orders, menu, staff)
    retention = analyze_retention(orders, menu, staff)
    operations = analyze_operations(orders, menu, staff)
    headlines = compute_headlines(integrity, retention, operations)

    body = owner_summary(headlines, venue_name=args.venue)
    print(f"from={settings.sender}  to={settings.recipient}  "
          f"dry_run={settings.dry_run}  sandbox={settings.is_sandbox}")
    print("--- message ---")
    print(body)
    print("---------------")

    notifier = WhatsAppNotifier(settings)
    try:
        if args.template:
            result = notifier.send_template(args.template, variables={
                "leakage": f"{headlines.monthly_leakage:,.0f}",
                "recoverable": f"{headlines.monthly_winback_tier_a:,.0f}",
            })
            print(f"Template send accepted: sid={result.sid} status={result.status}")
        else:
            result = notifier.send_and_confirm(body)
            print(f"DELIVERED: sid={result.sid} status={result.status}")
    except WhatsAppSendError as e:
        print(f"SEND FAILED [{e.category.value}] code={e.code}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
