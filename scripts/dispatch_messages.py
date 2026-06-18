#!/usr/bin/env python3
"""Approve and dispatch Customer Agent messages (merchant approval flow)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.merchant_customer import approve_and_send
from app.api.deps import build_merchant_dashboard, load_registry, outbox_path

DATA = Path(__file__).resolve().parent.parent / "data"
VENUE = "Sugar Rush"


def main() -> None:
    dash = build_merchant_dashboard()
    registry = load_registry()

    # Approve sendable messages for QR-linked guests with phone numbers
    refs = [
        p.customer_ref for p in dash.pending_comms
        if p.sendable
    ][:10]

    if not refs:
        print("No sendable messages pending approval.")
        print(f"  Pending total: {dash.headlines.pending_approval}")
        print(f"  Needs QR link: {dash.headlines.needs_qr_link}")
        return

    print(f"Approving {len(refs)} messages for {VENUE}...\n")
    sent, skipped = approve_and_send(dash, refs, outbox_path())

    print("=" * 50)
    print(f"Sent: {len(sent)}  Skipped: {len(skipped)}")
    print(f"Outbox: {outbox_path()}")


if __name__ == "__main__":
    main()
