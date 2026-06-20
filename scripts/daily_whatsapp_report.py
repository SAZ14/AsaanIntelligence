#!/usr/bin/env python3
"""End-of-day inventory report for owners, delivered over WhatsApp (Twilio).

Multi-venue: reads venues.toml and sends each owner their own venue's report.
If venues.toml is missing it falls back to the single `data/` folder.

By default it prints the messages it would send (dry run). Pass --send to
deliver via Twilio (account credentials come from the environment; each venue's
recipient comes from venues.toml).

Usage:
  python scripts/daily_whatsapp_report.py                      # dry run, all venues
  python scripts/daily_whatsapp_report.py --send               # send all venues
  python scripts/daily_whatsapp_report.py --venue "Sugar Rush" # just one venue
  python scripts/daily_whatsapp_report.py --as-of 2026-05-31   # pick the day
"""

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import VenueConfig, load_venues
from app.ingest import load_dataset
from app.ingest.loader import load_ingredients, load_recipes, load_stock_receipts
from app.agents.inventory import (
    compute_daily_movement,
    render_whatsapp_report,
    run_inventory_agent,
)


def build_message(data_dir: Path, venue_name: str, as_of_arg: str | None) -> str:
    orders, menu, staff = load_dataset(
        data_dir / "sales_detail.csv", data_dir / "menu.csv", data_dir / "staff.csv",
    )
    ingredients = load_ingredients(data_dir / "ingredients.csv")
    recipes = load_recipes(data_dir / "recipes.csv")
    receipts = load_stock_receipts(data_dir / "stock_receipts.csv")

    if as_of_arg:
        as_of = datetime.strptime(as_of_arg, "%Y-%m-%d").date()
    elif orders:
        as_of = max(o.datetime.date() for o in orders)
    else:
        as_of = date.today()

    report = run_inventory_agent(
        orders, menu, ingredients, recipes, receipts,
        venue_name=venue_name, with_reorder_plan=False,
    )
    movement = compute_daily_movement(orders, recipes, ingredients, as_of)
    return render_whatsapp_report(report, movement)


def venues_to_process(args) -> list[VenueConfig]:
    config_path = ROOT / "venues.toml"
    if config_path.exists():
        venues = load_venues(config_path)
    else:
        # Fall back to the single bundled data/ folder.
        venues = [VenueConfig(name="Sugar Rush", data_dir="data")]
    if args.venue:
        venues = [v for v in venues if v.name.lower() == args.venue.lower()]
        if not venues:
            sys.exit(f"No venue named {args.venue!r} in venues.toml")
    return venues


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true",
                        help="actually send via Twilio (otherwise print only)")
    parser.add_argument("--venue", help="only process this venue (by name)")
    parser.add_argument("--as-of", metavar="YYYY-MM-DD",
                        help="day to report on (default: latest day in the data)")
    args = parser.parse_args()

    for venue in venues_to_process(args):
        data_dir = venue.resolve_dir(ROOT)
        message = build_message(data_dir, venue.name, args.as_of)

        print("═" * 50)
        print(f"VENUE: {venue.name}  →  {venue.owner_whatsapp or '(no number set)'}")
        print("─" * 50)
        print(message)
        print("─" * 50)
        print(f"({len(message)} chars)")

        if args.send:
            from app.notify import send_whatsapp
            try:
                sid = send_whatsapp(message, to=venue.owner_whatsapp or None)
                print(f"Sent. Message SID: {sid}")
            except Exception as e:
                print(f"NOT sent for {venue.name}: {e}")
        else:
            print("Dry run — pass --send to deliver via Twilio.")
        print()


if __name__ == "__main__":
    main()
