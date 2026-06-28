"""Scheduled reputation sweep — scrapes reviews, classifies, drafts replies, alerts owners.

Run with:
    python scripts/reputation_live.py

Environment variables used (all optional with sensible fallbacks):
    APIFY_TOKEN, GOOGLE_MAPS_TERMS, GOOGLE_MAPS_LOCATION
    FOODPANDA_URL, FOODPANDA_KEYWORD, INSTAGRAM_USERNAMES
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_MERCHANT_FROM
    DRY_RUN=true  (skip actual Twilio sends)
    BRAND_VOICE_TONE, BRAND_VOICE_NEVER_SAY
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

load_dotenv(REPO_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("reputation_live")


def fetch_stores() -> list[dict]:
    from app.core.db import SessionLocal, Store

    with SessionLocal() as db:
        stores = db.query(Store).all()
        return [{"id": s.id, "name": s.name} for s in stores]


def fetch_store_owners(store_id: int) -> list[str]:
    """Return WhatsApp numbers for all owners of a store."""
    from app.core.db import SessionLocal, VenueConfig, StoreMember

    phones: list[str] = []

    with SessionLocal() as db:
        vc = db.query(VenueConfig).filter(VenueConfig.store_id == store_id).first()
        if vc and vc.owner_phones:
            phones.extend(vc.owner_phones)

        if not phones:
            members = (
                db.query(StoreMember)
                .filter(StoreMember.store_id == store_id, StoreMember.role == "owner")
                .all()
            )
            phones.extend(m.whatsapp for m in members)

    return list(dict.fromkeys(phones))


def fetch_store_twilio_number(store_id: int) -> str:
    """Return the store's Twilio WhatsApp number (used as FROM for owner alerts)."""
    from app.core.db import SessionLocal, StoreTwilioNumber

    with SessionLocal() as db:
        tn = (
            db.query(StoreTwilioNumber)
            .filter(StoreTwilioNumber.store_id == store_id)
            .first()
        )
        return (tn.whatsapp_number if tn else None) or os.environ.get("TWILIO_WHATSAPP_NUMBER", "")


def process_store_reviews(store: dict, wa_config=None) -> int:
    """Run the full review pipeline for one store. Returns count of new reviews saved."""
    from app.agents.reputation import (
        BrandVoice,
        ReviewAnalysis,
        classify_reviews_batch,
        correlate_review,
        draft_replies,
    )
    from app.core.llm import get_client
    from app.review_sources import db as review_db
    from app.review_sources.normalizer import to_review_model
    from app.review_sources.pipeline import run_pipeline
    from app.whatsapp.config import WhatsAppConfig
    from app.whatsapp.notifier import send_review_alert

    store_id: int = store["id"]
    store_name: str = store.get("name", "the venue")

    if wa_config is None:
        wa_config = WhatsAppConfig.from_env()

    raw_reviews = run_pipeline(store_id)
    if not raw_reviews:
        logger.info("No reviews found for store %s", store_name)
        return 0

    run_id = review_db.save_run(store_id, "reputation_live")
    client = get_client()

    # Brand voice from per-store ReputationConfig (no env-var hardcoding)
    from app.agents.reputation import _load_brand_voice
    brand = _load_brand_voice(store_id, store_name)

    analyses: list[tuple[dict, ReviewAnalysis]] = []
    for r in raw_reviews:
        try:
            rev_model = to_review_model(r)
            ctx = correlate_review(rev_model, [], {}, {})
            ra = ReviewAnalysis(
                review_id=rev_model.review_id,
                source=rev_model.source,
                rating=rev_model.rating,
                posted_at=str(rev_model.posted_at),
                reviewer_name=rev_model.reviewer_name,
                text=rev_model.text,
                correlation=ctx,
            )
            analyses.append((r, ra))
        except Exception as exc:
            logger.warning("Skipping review: %s", exc)

    classify_reviews_batch([ra for _, ra in analyses], client)
    draft_replies([ra for _, ra in analyses], client, venue_name=store_name, brand_voice=brand)

    owner_phones = fetch_store_owners(store_id)
    twilio_from = fetch_store_twilio_number(store_id)

    from app.whatsapp.config import WhatsAppConfig as _WA
    alert_cfg = _WA(
        account_sid=wa_config.account_sid,
        auth_token=wa_config.auth_token,
        from_number=twilio_from or wa_config.from_number,
        dry_run=wa_config.dry_run,
    )

    new_count = 0
    for raw, ra in analyses:
        ai_summary = {
            "status": "pending",
            "sentiment": ra.sentiment,
            "issue_class": ra.issue_class,
            "draft_reply": ra.draft_reply,
            "correlation": {
                "estimated_date": ra.correlation.estimated_date,
                "matched_staff_name": ra.correlation.matched_staff_name,
                "estimated_hour_range": ra.correlation.estimated_hour_range,
                "confidence": ra.correlation.confidence,
            },
        }

        saved = review_db.save_review_finding(
            store_id,
            run_id,
            store_name,
            raw,
            ai_summary,
            relevance_score=int((ra.rating or 0) * 20),
        )

        # Alert owners about negative/mixed reviews that need a response
        if saved and (ra.rating or 0) <= 3 and owner_phones:
            for phone in owner_phones:
                send_review_alert(phone, raw, ai_summary, config=alert_cfg)

        if saved:
            new_count += 1

    review_db.update_run(run_id, "ok", ["pipeline"], [], new_count)
    logger.info("Store %s: saved %d new reviews", store_name, new_count)
    return new_count


if __name__ == "__main__":
    stores = fetch_stores()
    if not stores:
        logger.error("No stores found in database.")
        sys.exit(1)

    total = 0
    for store in stores:
        try:
            count = process_store_reviews(store)
            total += count
        except Exception as exc:
            logger.error("Failed processing store %s: %s", store.get("name"), exc)

    logger.info(
        "Reputation live complete: %d total reviews across %d stores",
        total,
        len(stores),
    )
