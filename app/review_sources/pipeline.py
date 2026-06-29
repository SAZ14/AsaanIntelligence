from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

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
        "apify_api_key": os.environ.get("APIFY_TOKEN", ""),
        "google_maps_terms": rc.google_maps_terms or [],
        "google_maps_location": rc.google_maps_location or "",
        "foodpanda_url": rc.foodpanda_url or "",
        "foodpanda_keyword": rc.foodpanda_keyword or "",
        "instagram_usernames": rc.instagram_usernames or [],
    }


def run_pipeline(store_id: int | None = None) -> list[dict]:
    """Run all review scrapers for one store in parallel.

    If store_id is provided, config is loaded from the ReputationConfig DB table.
    If omitted, returns an empty list (all per-store config must live in the DB).
    """
    if store_id is None:
        log.warning("run_pipeline called without store_id — no reviews scraped")
        return []

    cfg = _load_config_from_db(store_id)
    key = cfg["apify_api_key"]

    # Build task map: source_name → callable
    tasks: dict[str, object] = {}
    if key and cfg["google_maps_terms"]:
        tasks["google_maps"] = lambda: fetch_maps(
            key, cfg["google_maps_terms"], cfg["google_maps_location"]
        )
    if key and cfg["foodpanda_url"]:
        tasks["foodpanda"] = lambda: fetch_foodpanda(
            key, cfg["foodpanda_url"], cfg["foodpanda_keyword"]
        )
    if key and cfg["instagram_usernames"]:
        tasks["instagram"] = lambda: fetch_instagram(key, cfg["instagram_usernames"])

    if not tasks:
        log.info("No scrape sources configured for store %d.", store_id)
        return []

    all_raw: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        future_to_name = {executor.submit(fn): name for name, fn in tasks.items()}
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                reviews = future.result()
                log.info("%s: %d reviews", name, len(reviews))
                all_raw.extend(reviews)
            except Exception as exc:
                log.error("%s scrape failed: %s", name, exc)

    if not all_raw:
        log.info("No reviews found for store %d.", store_id)
        return []

    return normalizer.normalize(all_raw)
