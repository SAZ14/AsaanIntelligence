#!/usr/bin/env python3
"""Live loyalty demo — simulate a customer scanning the QR and watch the
WhatsApp replies fill up their card, complete it, and promote them.

Each "scan" is what would happen when the customer sends the wa.me message.
The replies are pushed through the shared WhatsAppNotifier in DRY_RUN (default
ON), so this runs with no credentials and never hits the network.

    python scripts/customer_live.py [phone] [num_scans]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.customer import InMemoryCardStore, LoyaltyProgram, build_wa_link
from app.whatsapp import WhatsAppNotifier

BUSINESS_NUMBER = "whatsapp:+14155238886"


def main() -> None:
    phone = sys.argv[1] if len(sys.argv) > 1 else "+923001234567"
    scans = int(sys.argv[2]) if len(sys.argv) > 2 else 7

    program = LoyaltyProgram(venue_name="Sugar Rush", store=InMemoryCardStore())
    notifier = WhatsAppNotifier()  # DRY_RUN defaults ON

    print(f"QR encodes: {build_wa_link(BUSINESS_NUMBER)}")
    print(f"Simulating {scans} scans for {phone}\n" + "=" * 70)

    for i in range(1, scans + 1):
        result = program.record_scan(phone)
        notifier.send(phone, result.message)   # deliver the reply (dry-run)
        tag = ""
        if result.completed_tier:
            tag = f"  ← completed {result.completed_tier.name}, now on {result.promoted_to.name}"
        print(f"\nScan {i}{tag}")
        print("-" * 70)
        print(result.message)

    card = program.lookup(phone)
    pending = program.pending_rewards(card)
    print("\n" + "=" * 70)
    print(f"{len(pending)} reward(s) waiting to be collected: "
          f"{', '.join(r.reward for r in pending) or 'none'}")
    print(f"{len(notifier.sent)} WhatsApp message(s) "
          f"{'recorded (dry-run)' if notifier.dry_run else 'sent'}.")


if __name__ == "__main__":
    main()
