#!/usr/bin/env python3
"""End-of-day inventory report for the owner, delivered over WhatsApp (Twilio).

Run this once a day (e.g. from cron at closing time). By default it prints the
message it would send (dry run). Pass --send to actually deliver it via Twilio
using the credentials in the environment (see app/notify/whatsapp.py).

Usage:
  python scripts/daily_whatsapp_report.py                 # dry run (print only)
  python scripts/daily_whatsapp_report.py --send          # send via Twilio
  python scripts/daily_whatsapp_report.py --as-of 2026-05-31   # pick the day
"""

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest import load_dataset
from app.ingest.loader import load_ingredients, load_recipes, load_stock_receipts
from app.agents.inventory import (
    compute_daily_movement,
    render_whatsapp_report,
    run_inventory_agent,
)

DATA = Path(__file__).resolve().parent.parent / "data"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true",
                        help="actually send via Twilio (otherwise print only)")
    parser.add_argument("--as-of", metavar="YYYY-MM-DD",
                        help="day to report on (default: latest day in the data)")
    args = parser.parse_args()

    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )
    ingredients = load_ingredients(DATA / "ingredients.csv")
    recipes = load_recipes(DATA / "recipes.csv")
    receipts = load_stock_receipts(DATA / "stock_receipts.csv")

    if args.as_of:
        as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date()
    elif orders:
        as_of = max(o.datetime.date() for o in orders)
    else:
        as_of = date.today()

    report = run_inventory_agent(
        orders, menu, ingredients, recipes, receipts, with_reorder_plan=False,
    )
    movement = compute_daily_movement(orders, recipes, ingredients, as_of)
    message = render_whatsapp_report(report, movement)

    print("─" * 50)
    print(message)
    print("─" * 50)
    print(f"({len(message)} chars)")

    if args.send:
        from app.notify import send_whatsapp
        try:
            sid = send_whatsapp(message)
            print(f"\nSent via Twilio. Message SID: {sid}")
        except Exception as e:
            print(f"\nNOT sent: {e}")
            print("Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, "
                  "OWNER_WHATSAPP_TO and install twilio (pip install twilio).")
            sys.exit(1)
    else:
        print("\nDry run — pass --send to deliver via Twilio.")


if __name__ == "__main__":
    main()
