#!/usr/bin/env python3
"""Live Customer agent — run on the mock feed and alert the owner about lapsed VIPs.

Sends one WhatsApp alert per lapsed VIP to OWNER_NUMBER via the shared
``WhatsAppNotifier``. DRY_RUN is ON by default (set WHATSAPP_DRY_RUN=0 plus
Twilio credentials to actually send), so this stays safe in CI and on the
mock feed.

v1 SCOPE — flagging is not the same as contacting.
    We alert the *owner* that a valued regular has lapsed, described by
    behaviour and recovery-adjusted value. We do NOT message the customer:
    customer_ref is a tokenised payment id, not a phone number, and reaching
    the customer directly requires merchant-provided opt-in contact data
    (loyalty signup, etc.) which we do not have. That lookup is stubbed below.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest import load_dataset
from app.agents.customer import (
    CustomerAgentReport,
    format_lapsed_vip_alert,
    run_from_dataset,
)
from app.whatsapp import SentMessage, WhatsAppNotifier

DATA = Path(__file__).resolve().parent.parent / "data"
DEFAULT_OWNER = "whatsapp:+920000000000"


def _lookup_customer_contact(customer_ref: str) -> str | None:
    """STUB (v1): resolve a customer's opt-in contact from customer_ref.

    Returns None — we have no opt-in contact data for tokenised payment refs.
    Reaching the customer directly is out of scope for v1; we only alert the
    owner. Wire this to the merchant's loyalty/opt-in store in a later version.
    """
    return None


def send_lapsed_vip_alerts(
    report: CustomerAgentReport,
    notifier: WhatsAppNotifier,
    owner_number: str,
) -> list[SentMessage]:
    sent: list[SentMessage] = []
    for d in report.lapsed_vips:
        body = format_lapsed_vip_alert(d, venue_name=report.venue_name)
        sent.append(notifier.send(owner_number, body))
    return sent


def main() -> None:
    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )
    report = run_from_dataset(orders, menu, staff)

    notifier = WhatsAppNotifier()  # DRY_RUN defaults ON
    owner = os.environ.get("OWNER_NUMBER", DEFAULT_OWNER)

    mode = "DRY RUN (no messages sent)" if notifier.dry_run else "LIVE"
    print(f"Customer live alerts — {mode}")
    print(f"Lapsed VIPs to alert: {len(report.lapsed_vips)} → owner {owner}\n")

    sent = send_lapsed_vip_alerts(report, notifier, owner)
    for msg in sent:
        print("-" * 70)
        print(f"to={msg.to}  status={msg.status}")
        print(msg.body)
    print("-" * 70)
    print(f"\n{len(sent)} alert(s) {'recorded (dry-run)' if notifier.dry_run else 'sent'}.")


if __name__ == "__main__":
    main()
