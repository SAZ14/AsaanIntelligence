#!/usr/bin/env python3
"""Scrape review sources, run the reputation agent, and alert the owner on WhatsApp.

Set DRY_RUN=true (default) to print payloads instead of sending.

Usage:
    python scripts/reputation_live.py              # run once
    python scripts/reputation_live.py --daemon     # run every 12 hours
"""

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_dotenv
from app.ingest import load_dataset
from app.agents.reputation import DEFAULT_BRAND, run_reputation_agent
from app.review_sources import db as review_db
from app.review_sources import normalizer
from app.review_sources import pipeline
from whatsapp.config import WhatsAppConfig
from whatsapp.notifier import send_review_alert

DATA = Path(__file__).resolve().parent.parent / "data"
POLL_HOURS = 12

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("reputation_live")


def needs_attention(ra) -> bool:
    return ra.rating <= 3 or ra.sentiment in ("negative", "mixed")


def process_new_reviews(wa: WhatsAppConfig, owner: str) -> int:
    added = pipeline.run_pipeline()
    if not added:
        return 0

    all_reviews = review_db.get_recent_reviews(50)
    if not all_reviews:
        return added

    reviews = [normalizer.to_review_model(r) for r in all_reviews]

    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )

    log.info("Running reputation agent on %d reviews...", len(reviews))
    report = run_reputation_agent(reviews, orders, staff, menu, brand=DEFAULT_BRAND)

    flagged = [ra for ra in report.reviews if needs_attention(ra)]
    if not flagged:
        log.info("No flagged reviews among the %d new items.", added)
        return added

    log.info("Sending %d alert(s) to owner (dry_run=%s)...", len(flagged), wa.dry_run)
    for ra in flagged:
        send_review_alert(
            owner_number=owner,
            review=ra,
            correlation=ra.correlation,
            issue=ra.issue_class,
            draft=ra.draft_reply,
            config=wa,
        )

    if report.patterns:
        log.info("Detected %d pattern(s):", len(report.patterns))
        for p in report.patterns:
            log.info("  %s", p.description)

    return added


def run_once() -> None:
    load_dotenv()

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        log.error("No ANTHROPIC_API_KEY found.")
        sys.exit(1)

    wa = WhatsAppConfig.from_env()
    owner = os.environ.get("OWNER_NUMBER", "")
    if not owner:
        if wa.dry_run:
            owner = "<OWNER_NUMBER>"
        else:
            log.error("OWNER_NUMBER not set and DRY_RUN is off.")
            sys.exit(1)

    if not wa.dry_run:
        try:
            wa.require_twilio_credentials()
        except RuntimeError as e:
            log.error("%s", e)
            sys.exit(1)

    added = process_new_reviews(wa, owner)
    log.info("Done — %d new review(s) processed.", added)


def run_daemon() -> None:
    log.info("Starting daemon — polling every %d hours.", POLL_HOURS)
    load_dotenv()

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        log.error("No ANTHROPIC_API_KEY found.")
        sys.exit(1)

    wa = WhatsAppConfig.from_env()
    owner = os.environ.get("OWNER_NUMBER", "<OWNER_NUMBER>")

    while True:
        try:
            added = process_new_reviews(wa, owner)
            log.info("Poll cycle complete — %d new review(s).", added)
        except Exception as e:
            log.error("Poll cycle failed: %s", e)

        time.sleep(POLL_HOURS * 3600)


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        run_daemon()
    else:
        run_once()
