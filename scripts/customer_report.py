#!/usr/bin/env python3
"""Staff-facing loyalty tool — manual lookup and programme overview.

Multi-restaurant aware: reads the restaurants config (env RESTAURANTS_CONFIG,
default restaurants.json) and the per-venue stores under LOYALTY_DIR — the same
data the webhook writes.

    python scripts/customer_report.py                                  # all venues
    python scripts/customer_report.py --restaurant sugar_rush          # one venue
    python scripts/customer_report.py --restaurant sugar_rush +923001234567
    python scripts/customer_report.py --restaurant sugar_rush +923001234567 --redeem
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.customer import Restaurant, build_default_registry, format_card_status


def _venue_overview(r: Restaurant) -> None:
    cards = r.program.store.all()
    pending = [(c, r.program.pending_rewards(c)) for c in cards]
    waiting = sum(len(p) for _, p in pending)
    print("=" * 70)
    print(f"{r.name}  (id: {r.id}, {r.whatsapp_number})")
    print("-" * 70)
    print(f"  Members: {len(cards)}   "
          f"Lifetime scans: {sum(c.total_scans for c in cards)}   "
          f"Rewards waiting: {waiting}")
    for c, p in pending:
        if p:
            print(f"    {c.phone:18s} {', '.join(x.reward for x in p)}")


def main() -> None:
    registry = build_default_registry()
    rest_id = None
    phones = []
    redeem = False
    it = iter(sys.argv[1:])
    for a in it:
        if a == "--restaurant":
            rest_id = next(it, None)
        elif a == "--redeem":
            redeem = True
        elif not a.startswith("--"):
            phones.append(a)

    # Programme overview (all venues, or just one).
    if not phones:
        venues = [registry.by_id(rest_id)] if rest_id else registry.all()
        if venues == [None]:
            sys.exit(f"No restaurant '{rest_id}'. Known: {[r.id for r in registry.all()]}")
        for r in venues:
            _venue_overview(r)
        return

    # Single-customer lookup needs a venue (loyalty is per-restaurant).
    if rest_id is None and len(registry.all()) == 1:
        rest_id = registry.all()[0].id
    if rest_id is None:
        sys.exit(f"Specify --restaurant <id>. Known: {[r.id for r in registry.all()]}")
    r = registry.by_id(rest_id)
    if r is None:
        sys.exit(f"No restaurant '{rest_id}'. Known: {[x.id for x in registry.all()]}")

    phone = phones[0]
    card = r.program.lookup(phone)
    if card is None:
        print(f"No loyalty card for {phone} at {r.name}.")
        return
    print(format_card_status(card, r.program))
    if redeem:
        given = r.program.redeem(phone)
        print(f"\n→ Marked '{given.reward}' as handed over." if given
              else "\n→ Nothing pending to redeem.")


if __name__ == "__main__":
    main()
