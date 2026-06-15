#!/usr/bin/env python3
"""Run the Reputation agent on the reviews feed and alert the owner on WhatsApp.

For each review that needs attention (low rating or negative/mixed sentiment),
formats an interactive WhatsApp alert with the drafted reply and three buttons
and sends it to OWNER_NUMBER via the Cloud API.

Set DRY_RUN=true (the default) to print the exact payloads instead of sending.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_dotenv
from app.ingest import load_dataset
from app.ingest.loader import load_reviews
from app.agents.reputation import DEFAULT_BRAND, run_reputation_agent
from whatsapp.config import WhatsAppConfig
from whatsapp.notifier import send_review_alert

DATA = Path(__file__).resolve().parent.parent / "data"


def needs_attention(ra) -> bool:
    return ra.rating <= 3 or ra.sentiment in ("negative", "mixed")


def main() -> None:
    load_dotenv()

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("ERROR: no ANTHROPIC_API_KEY found (environment or .env).", file=sys.stderr)
        sys.exit(1)

    wa = WhatsAppConfig.from_env()
    owner = os.environ.get("OWNER_NUMBER", "")
    if not owner:
        if wa.dry_run:
            owner = "<OWNER_NUMBER>"  # placeholder is fine — DRY_RUN only prints
        else:
            print("ERROR: OWNER_NUMBER not set and DRY_RUN is off.", file=sys.stderr)
            sys.exit(1)

    # Mock feed: the bundled reviews CSV stands in for a live review source.
    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )
    reviews = load_reviews(DATA / "reviews.csv")

    print(f"Loaded {len(reviews)} reviews. Running agent for {DEFAULT_BRAND.name}...")
    report = run_reputation_agent(reviews, orders, staff, menu, brand=DEFAULT_BRAND)

    flagged = [ra for ra in report.reviews if needs_attention(ra)]
    print(f"{len(flagged)} review(s) need attention. "
          f"DRY_RUN={wa.dry_run} → {'printing' if wa.dry_run else 'sending'} alerts to {owner}\n")

    for ra in flagged:
        send_review_alert(
            owner_number=owner,
            review=ra,
            correlation=ra.correlation,
            issue=ra.issue_class,
            draft=ra.draft_reply,
            config=wa,
        )
        print()


if __name__ == "__main__":
    main()
