#!/usr/bin/env python3
import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_dotenv, REPO_ROOT
from app.agents.reputation import BrandVoice, run_reputation_agent
from app.review_sources import db as review_db
from app.review_sources import pipeline
from app.review_sources.normalizer import to_review_model
from app.whatsapp.config import WhatsAppConfig
from app.whatsapp.notifier import send_review_alert

POLL_HOURS = 12

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("reputation_live")


def _supabase():
    try:
        from app.database import supabase
        return supabase
    except Exception:
        return None


def needs_attention(ra) -> bool:
    return ra.rating <= 3 or ra.sentiment in ("negative", "mixed")


def fetch_stores() -> list[dict]:
    client = _supabase()
    if client is not None and os.environ.get("ASAAN_TEST_MODE") != "1":
        res = client.table("stores").select("*").execute()
        return res.data or []
    else:
        # Fallback to env-configured default store
        return [{
            "id": 1,
            "name": os.environ.get("VENUE_NAME", "Sugar Rush"),
            "config": {
                "google_maps_terms": os.environ.get("GOOGLE_MAPS_TERMS", ""),
                "google_maps_location": os.environ.get("GOOGLE_MAPS_LOCATION", ""),
                "foodpanda_url": os.environ.get("FOODPANDA_URL", ""),
                "foodpanda_keyword": os.environ.get("FOODPANDA_KEYWORD", ""),
                "instagram_usernames": os.environ.get("INSTAGRAM_USERNAMES", ""),
                "pos_connection": {
                    "pos_type": "csv",
                    "connection": {"base_dir": "data"},
                    "mapping": "cafe_generic"
                }
            }
        }]


def fetch_store_owners(store_id: int) -> list[str]:
    client = _supabase()
    if client is not None and os.environ.get("ASAAN_TEST_MODE") != "1":
        res = client.table("store_members").select("whatsapp").eq("store_id", store_id).eq("role", "owner").execute()
        return [row["whatsapp"] for row in res.data] if res.data else []
    else:
        owner = os.environ.get("OWNER_NUMBER", "")
        return [owner] if owner else []


def process_store_reviews(store: dict, wa: WhatsAppConfig) -> int:
    store_id = store["id"]
    store_name = store["name"]
    store_config = store.get("config") or {}
    
    log.info("Processing reviews for store: %s (ID: %d)...", store_name, store_id)

    # 1. Run scraper pipeline
    scraped_reviews = pipeline.run_pipeline(store_config)
    if not scraped_reviews:
        log.info("No reviews found for %s.", store_name)
        return 0

    # 2. Filter out already processed reviews
    client = _supabase()
    new_reviews_data = []
    if client is not None and os.environ.get("ASAAN_TEST_MODE") != "1":
        hashes = [r["hash"] for r in scraped_reviews]
        # In Postgrest, we can filter using `in_` for finding matches
        res = client.table("findings").select("content_hash").eq("store_id", store_id).in_("content_hash", hashes).execute()
        existing_hashes = {row["content_hash"] for row in res.data} if res.data else set()
        new_reviews_data = [r for r in scraped_reviews if r["hash"] not in existing_hashes]
    else:
        new_reviews_data = scraped_reviews

    if not new_reviews_data:
        log.info("All scraped reviews for %s are already processed.", store_name)
        return 0

    log.info("Found %d new reviews for %s. Loading POS data for visit correlation...", len(new_reviews_data), store_name)

    # 3. Load POS data using Connector registry
    from app.pos.base import RestaurantConfig, build_connector
    pos_config = store_config.get("pos_connection") or {}
    conn_params = pos_config.get("connection") or {"base_dir": "data"}
    
    # If using relative path for CSV base_dir, resolve relative to REPO_ROOT
    if pos_config.get("pos_type", "csv") == "csv" and "base_dir" in conn_params:
        base_dir = Path(conn_params["base_dir"])
        if not base_dir.is_absolute():
            conn_params["base_dir"] = str(REPO_ROOT / base_dir)

    restaurant_cfg = RestaurantConfig(
        venue_name=store_name,
        pos_type=pos_config.get("pos_type", "csv"),
        connection=conn_params,
        mapping=pos_config.get("mapping", "cafe_generic")
    )
    
    try:
        connector = build_connector(restaurant_cfg)
        pos_data = connector.fetch()
    except Exception as e:
        log.error("Failed to load POS data for %s: %s", store_name, e)
        return 0

    # 4. Save run log to database
    run_id = review_db.save_run(store_id, "reputation")

    # 5. Convert reviews to canonical model and run reputation agent
    reviews_models = [to_review_model(r) for r in new_reviews_data]
    brand = BrandVoice(
        name=store_name,
        tone=store_config.get("brand_tone", BrandVoice.tone),
        never_say=store_config.get("brand_never_say", BrandVoice().never_say)
    )

    from app.agents.reputation import ZaiClient
    zai_client = ZaiClient()

    report = run_reputation_agent(
        reviews=reviews_models,
        orders=pos_data.orders,
        staff=pos_data.staff,
        menu=pos_data.menu,
        brand=brand,
        client=zai_client
    )

    # 6. Save each finding and alert owners
    owners = fetch_store_owners(store_id)
    added_count = 0

    for ra in report.reviews:
        # Match back to the original dictionary for serialization
        orig_review = next((r for r in new_reviews_data if r["hash"] == ra.review_id), None)
        if not orig_review:
            continue

        ai_summary = {
            "status": "pending",
            "sentiment": ra.sentiment,
            "issue_class": ra.issue_class,
            "draft_reply": ra.draft_reply,
            "correlation": {
                "confidence": ra.correlation.confidence,
                "estimated_date": ra.correlation.estimated_date,
                "estimated_hour_range": ra.correlation.estimated_hour_range,
                "order_count_in_window": ra.correlation.order_count_in_window,
                "matched_staff_name": ra.correlation.matched_staff_name,
                "match_reasons": ra.correlation.match_reasons
            }
        }

        # Save to database
        saved = review_db.save_review_finding(
            store_id=store_id,
            run_id=run_id,
            store_name=store_name,
            review=orig_review,
            ai_summary=ai_summary,
            relevance_score=0
        )
        if saved:
            added_count += 1

            # Check if alert is needed
            if needs_attention(ra):
                log.info("Review %s flags issue '%s'. Alerting %d owner(s)...", ra.review_id, ra.issue_class, len(owners))
                for owner in owners:
                    send_review_alert(
                        owner_number=owner,
                        review=ra,
                        correlation=ra.correlation,
                        issue=ra.issue_class,
                        draft=ra.draft_reply,
                        config=wa
                    )

    # 7. Update run details
    review_db.update_run(
        run_id=run_id,
        status="completed",
        sources_ok=list(set(r["source"] for r in scraped_reviews)),
        sources_failed=[],
        finding_count=added_count
    )

    log.info("Store %s processed: %d findings recorded, %d alerts sent.", store_name, added_count, len(report.reviews))
    return added_count


def run(daemon: bool = False) -> None:
    load_dotenv()
    
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        log.error("No ANTHROPIC_API_KEY found.")
        sys.exit(1)

    wa = WhatsAppConfig.from_env()

    if daemon:
        log.info("Starting reputation daemon - polling every %d hours.", POLL_HOURS)
        while True:
            try:
                stores = fetch_stores()
                for store in stores:
                    process_store_reviews(store, wa)
            except Exception as e:
                log.error("Daemon cycle failed: %s", e)
            time.sleep(POLL_HOURS * 3600)
    else:
        stores = fetch_stores()
        total_added = 0
        for store in stores:
            try:
                total_added += process_store_reviews(store, wa)
            except Exception as e:
                log.error("Failed processing store %s: %s", store.get("name"), e)
        log.info("Completed execution. Total new findings recorded: %d", total_added)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-tenant Reputation Agent Runner")
    parser.add_argument("--daemon", action="store_true", help="Run as a daemon polling every 12 hours")
    args = parser.parse_args()
    run(daemon=args.daemon)
