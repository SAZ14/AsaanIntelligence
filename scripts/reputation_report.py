#!/usr/bin/env python3
"""Run the Reputation agent on the main dataset and print the report."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_dotenv
from app.ingest import load_dataset
from app.ingest.loader import load_reviews
from app.agents.reputation import DEFAULT_BRAND, run_reputation_agent

DATA = Path(__file__).resolve().parent.parent / "data"


def main() -> None:
    load_dotenv()
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print(
            "ERROR: no ANTHROPIC_API_KEY found (looked in the environment and .env).\n"
            "Add it to a .env file at the repo root, e.g.:\n"
            "    ANTHROPIC_API_KEY=sk-ant-...\n"
            "then re-run this script.",
            file=sys.stderr,
        )
        sys.exit(1)

    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )
    reviews = load_reviews(DATA / "reviews.csv")

    print(f"Loaded {len(reviews)} reviews, {len(orders)} orders")
    print(f"Brand voice: {DEFAULT_BRAND.name}")
    print("Running reputation agent (Haiku=classify, Sonnet=draft)...\n")

    report = run_reputation_agent(reviews, orders, staff, menu, brand=DEFAULT_BRAND)

    print("=" * 70)
    print(f"REPUTATION REPORT — {report.venue_name}")
    print("=" * 70)
    print(f"  Reviews: {report.total_reviews}  |  Avg rating: {report.avg_rating:.1f}/5")

    print("\n" + "=" * 70)
    print("PER-REVIEW ANALYSIS")
    print("=" * 70)
    for ra in report.reviews:
        conf = ra.correlation.confidence
        match_info = "NO MATCH" if conf == "none" else f"{conf.upper()} → {ra.correlation.estimated_date} {ra.correlation.estimated_hour_range}"
        if ra.correlation.matched_staff_name:
            match_info += f" [staff: {ra.correlation.matched_staff_name}]"

        print(f"\n  {ra.review_id} | {ra.source:10s} | {ra.rating}/5 | {ra.reviewer_name}")
        print(f"  Text: {ra.text}")
        print(f"  Correlation: {match_info}")
        if ra.correlation.match_reasons:
            print(f"  Reasons: {'; '.join(ra.correlation.match_reasons)}")
        if ra.correlation.order_count_in_window > 0:
            print(f"  Window load: {ra.correlation.order_count_in_window} orders")
        print(f"  Issue: {ra.issue_class} | Sentiment: {ra.sentiment}")
        if ra.draft_reply:
            print(f"  Drafted reply: {ra.draft_reply}")

    print("\n" + "=" * 70)
    print("PATTERN FINDINGS")
    print("=" * 70)
    if report.patterns:
        for p in report.patterns:
            print(f"\n  [{p.review_count} reviews] {p.description}")
            print(f"  Review IDs: {', '.join(p.review_ids)}")
    else:
        print("  No recurring patterns detected.")

    print("\n" + "=" * 70)
    print(f"HAPPY REVIEWERS ({len(report.happy_reviewers)} — solicit for more reviews)")
    print("=" * 70)
    for h in report.happy_reviewers:
        print(f"  {h['reviewer_name']:15s}  {h['review_id']}  {h['rating']}/5  ({h['source']})")


if __name__ == "__main__":
    main()
