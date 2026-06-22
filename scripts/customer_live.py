#!/usr/bin/env python3
"""Live loyalty demo — simulate a customer scanning a restaurant's QR and watch
the WhatsApp replies fill up their single-tier card, complete it (free reward),
then start a fresh card.

Each "scan" is what happens when the customer sends the wa.me message. Replies
go through the shared WhatsAppNotifier in DRY_RUN (default ON), so this runs
with no credentials and never hits the network.

    python scripts/customer_live.py [phone] [num_scans]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.customer import build_restaurant
from app.whatsapp import WhatsAppNotifier


def main() -> None:
    phone = sys.argv[1] if len(sys.argv) > 1 else "+923001234567"
    scans = int(sys.argv[2]) if len(sys.argv) > 2 else 7

    restaurant = build_restaurant(
        id="sugar_rush", name="Sugar Rush",
        whatsapp_number="whatsapp:+14155238886",
        stamps_required=5, reward="a free ice cream",
    )
    program = restaurant.program
    notifier = WhatsAppNotifier()  # DRY_RUN defaults ON

    print(f"Restaurant: {restaurant.name}")
    print(f"QR encodes: {restaurant.wa_link()}")
    print(f"Simulating {scans} scans for {phone}\n" + "=" * 70)

    for i in range(1, scans + 1):
        result = program.record_scan(phone)
        notifier.send(phone, result.message)   # deliver the reply (dry-run)
        tag = "  ← card complete, reward unlocked, fresh card started" if result.completed_tier else ""
        print(f"\nScan {i}{tag}")
        print("-" * 70)
        print(result.message)

    pending = program.pending_rewards(program.lookup(phone))
    print("\n" + "=" * 70)
    print(f"{len(pending)} reward(s) waiting to be collected: "
          f"{', '.join(r.reward for r in pending) or 'none'}")
    print(f"{len(notifier.sent)} WhatsApp message(s) "
          f"{'recorded (dry-run)' if notifier.dry_run else 'sent'}.")


if __name__ == "__main__":
    main()
