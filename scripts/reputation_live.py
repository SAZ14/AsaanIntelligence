#!/usr/bin/env python3
"""Run the Reputation agent on data/reviews.csv and send WhatsApp alerts
for reviews that need the owner's attention.

Reads credentials from .env. With DRY_RUN=1 the alerts are printed instead
of sent, so this is runnable without Twilio credentials (the Anthropic key
is still required, since the agent classifies and drafts replies).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_env

load_env()  # populate os.environ from .env before anything reads keys

import os

from app.agents.reputation import ReviewAnalysis, run_reputation_agent
from app.ingest import load_dataset
from app.ingest.loader import load_reviews
from app.whatsapp.notifier import send_review_alert

DATA = Path(__file__).resolve().parent.parent / "data"


def needs_attention(ra: ReviewAnalysis) -> bool:
    """A review the owner should see: low rating or non-positive sentiment."""
    return ra.rating <= 3 or ra.sentiment in ("negative", "mixed")


def main() -> None:
    owner = os.environ.get("OWNER_NUMBER", "").strip()
    if not owner:
        print("OWNER_NUMBER not set in .env — cannot address alerts.", file=sys.stderr)
        sys.exit(1)

    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )
    reviews = load_reviews(DATA / "reviews.csv")

    print(f"Loaded {len(reviews)} reviews, {len(orders)} orders")
    print("Running reputation agent (calls Claude API)...\n")
    report = run_reputation_agent(reviews, orders, staff, menu)

    flagged = [ra for ra in report.reviews if needs_attention(ra)]
    print(f"{len(flagged)} of {report.total_reviews} reviews need attention "
          f"→ alerting {owner}\n")

    sent = 0
    for ra in flagged:
        result = send_review_alert(
            owner_number=owner,
            review=ra,                    # ReviewAnalysis carries source/rating/text/name
            correlation=ra.correlation,
            issue=ra.issue_class,
            draft=ra.draft_reply,
            sentiment=ra.sentiment,
        )
        if result.sent:
            sent += 1
            print(f"  sent {ra.review_id} → SID {result.sid}")

    if sent:
        print(f"\nSent {sent} alert(s).")
    else:
        print("\nDRY_RUN — nothing sent. Set DRY_RUN=0 with Twilio creds to deliver.")


if __name__ == "__main__":
    main()
