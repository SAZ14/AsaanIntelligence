#!/usr/bin/env python3
"""Dispatch Customer Agent incentive messages via SMS/WhatsApp outbox."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest import load_dataset
from app.ingest.loader import load_customers
from app.agents.customer import run_customer_agent
from app.services.messaging import ConsoleMessageDispatcher, FileOutboxDispatcher, dispatch_incentives

DATA = Path(__file__).resolve().parent.parent / "data"
OUT = Path(__file__).resolve().parent.parent / "output"
VENUE = "Sugar Rush"


def main() -> None:
    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )
    registry = load_customers(DATA / "customers.csv")
    report = run_customer_agent(orders, menu, staff, registry, venue_name=VENUE)

    print(f"Dispatching up to 10 incentive messages for {VENUE}...\n")
    outbox = OUT / "messages_outbox.jsonl"
    dispatcher = FileOutboxDispatcher(outbox, ConsoleMessageDispatcher())
    result = dispatch_incentives(report.incentives, dispatcher, require_phone=True, limit=10)

    print("=" * 50)
    print(f"Sent: {len(result.sent)}  Skipped: {len(result.skipped)}  Failed: {len(result.failed)}")
    print(f"Outbox: {outbox}")


if __name__ == "__main__":
    main()
