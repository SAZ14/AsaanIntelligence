from __future__ import annotations

import logging
import os

from app.config import load_dotenv
from app.review_sources import db, normalizer
from app.review_sources.foodpanda import fetch_reviews as fetch_foodpanda
from app.review_sources.google_maps import fetch_reviews as fetch_maps
from app.review_sources.instagram import fetch_reviews as fetch_instagram

log = logging.getLogger("review_sources.pipeline")


def _load_config() -> dict:
    return {
        "apify_api_key": os.environ.get("APIFY_API_KEY", ""),
        "google_maps_terms": [
            q.strip() for q in
            os.environ.get("GOOGLE_MAPS_TERMS", "[]").strip("[]").split(",")
            if q.strip()
        ],
        "google_maps_location": os.environ.get("GOOGLE_MAPS_LOCATION", ""),
        "foodpanda_url": os.environ.get("FOODPANDA_URL", ""),
        "foodpanda_keyword": os.environ.get("FOODPANDA_KEYWORD", ""),
        "instagram_usernames": [
            u.strip() for u in
            os.environ.get("INSTAGRAM_USERNAMES", "[]").strip("[]").split(",")
            if u.strip()
        ],
    }


def run_pipeline() -> int:
    load_dotenv()
    cfg = _load_config()
    db.init_db()

    all_raw: list[dict] = []

    if cfg["apify_api_key"] and cfg["google_maps_terms"]:
        try:
            reviews = fetch_maps(
                cfg["apify_api_key"],
                cfg["google_maps_terms"],
                cfg["google_maps_location"],
            )
            log.info("Google Maps: %d reviews", len(reviews))
            all_raw.extend(reviews)
        except Exception as e:
            log.error("Google Maps scrape failed: %s", e)

    if cfg["apify_api_key"] and cfg["foodpanda_url"]:
        try:
            reviews = fetch_foodpanda(
                cfg["apify_api_key"],
                cfg["foodpanda_url"],
                cfg["foodpanda_keyword"],
            )
            log.info("FoodPanda: %d reviews", len(reviews))
            all_raw.extend(reviews)
        except Exception as e:
            log.error("FoodPanda scrape failed: %s", e)

    if cfg["apify_api_key"] and cfg["instagram_usernames"]:
        try:
            reviews = fetch_instagram(cfg["apify_api_key"], cfg["instagram_usernames"])
            log.info("Instagram: %d items", len(reviews))
            all_raw.extend(reviews)
        except Exception as e:
            log.error("Instagram scrape failed: %s", e)

    if not all_raw:
        log.info("No reviews found from any source.")
        return 0

    normalized = normalizer.normalize(all_raw)
    added = db.save_reviews(normalized)
    log.info("DB: %d new reviews (out of %d scraped)", added, len(normalized))
    return added
