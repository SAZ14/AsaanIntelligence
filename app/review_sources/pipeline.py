from __future__ import annotations

import logging
import os

from app.review_sources import normalizer
from app.review_sources.foodpanda import fetch_reviews as fetch_foodpanda
from app.review_sources.google_maps import fetch_reviews as fetch_maps
from app.review_sources.instagram import fetch_reviews as fetch_instagram

log = logging.getLogger("review_sources.pipeline")


def _load_config_from_db(store_id: int) -> dict:
    """Fetch per-store scraping config from ReputationConfig table."""
    from app.core.db import SessionLocal, ReputationConfig

    with SessionLocal() as db:
        rc = db.query(ReputationConfig).filter(ReputationConfig.store_id == store_id).first()

    if rc is None:
        log.warning("No ReputationConfig for store %d — reviews cannot be scraped", store_id)
        return {
            "apify_api_key": os.environ.get("APIFY_TOKEN", ""),
            "google_maps_terms": [],
            "google_maps_location": "",
            "foodpanda_url": "",
            "foodpanda_keyword": "",
            "instagram_usernames": [],
        }

    return {
        "apify_api_key": os.environ.get("APIFY_TOKEN", ""),  # global credential, not per-store
        "google_maps_terms": rc.google_maps_terms or [],
        "google_maps_location": rc.google_maps_location or "",
        "foodpanda_url": rc.foodpanda_url or "",
        "foodpanda_keyword": rc.foodpanda_keyword or "",
        "instagram_usernames": rc.instagram_usernames or [],
    }


def run_pipeline(store_id: int | None = None) -> list[dict]:
    """Run all review scrapers for one store.

    If store_id is provided, config is loaded from the ReputationConfig DB table.
    If omitted, returns an empty list (all per-store config must live in the DB).
    """
    if store_id is None:
        log.warning("run_pipeline called without store_id — no reviews scraped")
        return []

    cfg = _load_config_from_db(store_id)
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
        log.info("No reviews found for store %d.", store_id)
        return []

    return normalizer.normalize(all_raw)
