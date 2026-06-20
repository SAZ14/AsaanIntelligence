#!/usr/bin/env python3
"""Staff-facing loyalty tool — manual lookup and programme overview.

Reads the same persistent store the webhook writes to (LOYALTY_STORE env var,
default ``loyalty_cards.json``).

    python scripts/customer_report.py                 # programme overview
    python scripts/customer_report.py +923001234567   # look one customer up
    python scripts/customer_report.py +923001234567 --redeem   # mark reward given
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.customer import JsonCardStore, LoyaltyProgram, format_card_status

STORE_PATH = Path(os.environ.get("LOYALTY_STORE", "loyalty_cards.json"))
VENUE_NAME = os.environ.get("VENUE_NAME", "Sugar Rush")


def _overview(program: LoyaltyProgram) -> None:
    cards = program.store.all()
    pending = [(c, program.pending_rewards(c)) for c in cards]
    rewards_waiting = sum(len(p) for _, p in pending)
    total_scans = sum(c.total_scans for c in cards)

    print("=" * 70)
    print(f"LOYALTY PROGRAMME — {VENUE_NAME}")
    print("=" * 70)
    print(f"  Members (unique WhatsApp numbers): {len(cards)}")
    print(f"  Lifetime scans:                    {total_scans}")
    print(f"  Rewards waiting to be handed over: {rewards_waiting}")
    if rewards_waiting:
        print("\n  Customers with rewards to collect:")
        for c, p in pending:
            if p:
                print(f"    {c.phone:18s} {', '.join(r.reward for r in p)}")
    print("\n  Tip: pass a phone number to look a single customer up.")


def main() -> None:
    program = LoyaltyProgram(venue_name=VENUE_NAME, store=JsonCardStore(STORE_PATH))
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    redeem = "--redeem" in sys.argv

    if not args:
        _overview(program)
        return

    phone = args[0]
    card = program.lookup(phone)
    if card is None:
        print(f"No loyalty card found for {phone}.")
        return

    print(format_card_status(card, program))
    if redeem:
        given = program.redeem(phone)
        print(f"\n→ Marked '{given.reward}' as handed over." if given
              else "\n→ Nothing pending to redeem.")


if __name__ == "__main__":
    main()
