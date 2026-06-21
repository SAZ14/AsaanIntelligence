#!/usr/bin/env python3
"""Re-engage lapsed loyal customers, and find VIPs for event invites.

Run this WHEN YOU WANT (manually, or on a schedule you choose) — it is not
automatic. DRY_RUN is ON by default, so it previews messages without sending
until you set WHATSAPP_DRY_RUN=0 (and, in production, register the wording as a
Meta-approved WhatsApp template — these are proactive messages outside the 24h
window).

    # Preview who'd get a "we miss you" nudge (no send, no marking):
    python scripts/customer_engage.py --restaurant sugar_rush

    # Actually send the nudges and mark them so they aren't re-spammed:
    python scripts/customer_engage.py --restaurant sugar_rush --send

    # List the 10 most loyal customers (for an event guest list):
    python scripts/customer_engage.py --restaurant sugar_rush --top 10

    # Invite the top 10 loyal customers to an event:
    python scripts/customer_engage.py --restaurant sugar_rush --top 10 \
        --invite "Tasting night this Friday 7pm, on the house."
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.customer import (
    at_risk_loyal_customers,
    build_default_registry,
    send_event_invites,
    send_reengagement,
    top_loyal_customers,
)
from app.whatsapp import WhatsAppNotifier


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--restaurant", help="restaurant id (default: all venues)")
    ap.add_argument("--inactive-days", type=int,
                    help="override: quiet for N days → nudge (default: the venue's setting)")
    ap.add_argument("--min-scans", type=int,
                    help="override: 'loyal' = at least N scans (default: the venue's setting)")
    ap.add_argument("--send", action="store_true", help="actually send the nudges + mark them")
    ap.add_argument("--top", type=int, help="list the top N loyal customers")
    ap.add_argument("--invite", help="event text; sends an invite to the --top N loyal customers")
    args = ap.parse_args()

    registry = build_default_registry()
    venues = [registry.by_id(args.restaurant)] if args.restaurant else registry.all()
    if venues == [None]:
        sys.exit(f"No restaurant '{args.restaurant}'. Known: {[r.id for r in registry.all()]}")

    notifier = WhatsAppNotifier()  # DRY_RUN defaults ON
    mode = "DRY RUN (nothing sent)" if notifier.dry_run else "LIVE"

    for r in venues:
        prog = r.program
        print("=" * 72)
        print(f"{r.name}  (id: {r.id})   — {mode}")
        print("=" * 72)

        # Event invites to the most loyal customers.
        if args.top and args.invite:
            tops = top_loyal_customers(prog, args.top)
            sent = send_event_invites(notifier, r.name, tops, args.invite)
            print(f"Invited {len(sent)} loyal customer(s):")
            for c, m in zip(tops, sent):
                print(f"  {c.phone:18s} ({c.total_scans} scans)  [{m.status}]")
            continue

        # Just list the VIPs.
        if args.top:
            tops = top_loyal_customers(prog, args.top)
            print(f"Top {len(tops)} loyal customers (by lifetime scans):")
            for c in tops:
                print(f"  {c.phone:18s} {c.total_scans:>3} scans, {len(c.rewards)} reward(s) earned")
            continue

        # Re-engagement nudges. Use this venue's owner-set policy unless the
        # CLI overrides it.
        inactive_days = args.inactive_days if args.inactive_days is not None else r.inactive_days
        min_scans = args.min_scans if args.min_scans is not None else r.min_scans
        cands = at_risk_loyal_customers(
            prog, min_scans=min_scans, inactive_days=inactive_days,
            cooldown_days=r.nudge_cooldown_days,
        )
        print(f"{len(cands)} loyal customer(s) quiet for ≥{inactive_days} days "
              f"(venue policy: ≥{r.min_scans} scans = loyal):")
        if args.send:
            send_reengagement(prog, notifier, cands)
        for c in cands:
            flag = " [sent]" if args.send else ""
            print(f"\n  {c.card.phone} — {c.card.total_scans} scans, "
                  f"{c.days_inactive} days quiet{flag}")
            print("  " + c.message.replace("\n", "\n  "))
        if cands and not args.send:
            print("\n(preview only — re-run with --send to deliver and mark them)")


if __name__ == "__main__":
    main()
