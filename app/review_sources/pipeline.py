from __future__ import annotations

import logging
import os

from app.review_sources import normalizer
from app.review_sources.foodpanda import fetch_reviews as fetch_foodpanda
from app.review_sources.google_maps import fetch_reviews as fetch_maps
from app.review_sources.instagram import fetch_reviews as fetch_instagram

log = logging.getLogger("review_sources.pipeline")


def _load_config(store_config: dict | None = None) -> dict:
    sc = store_config or {}
    # Unified token — same APIFY_TOKEN used across all Apify calls in this project
    apify_key = sc.get("apify_api_key") or os.environ.get("APIFY_TOKEN", "")

    maps_terms = sc.get("google_maps_terms")
    if not maps_terms:
        env_val = os.environ.get("GOOGLE_MAPS_TERMS", "")
        if env_val:
            maps_terms = [
                q.strip()
                for q in env_val.replace("'", "").replace('"', "").strip("[]").split(",")
                if q.strip()
            ]
        else:
            maps_terms = []

    maps_location = sc.get("google_maps_location") or os.environ.get("GOOGLE_MAPS_LOCATION", "")
    foodpanda_url = sc.get("foodpanda_url") or os.environ.get("FOODPANDA_URL", "")
    foodpanda_keyword = sc.get("foodpanda_keyword") or os.environ.get("FOODPANDA_KEYWORD", "")

    instagram_usernames = sc.get("instagram_usernames")
    if not instagram_usernames:
        env_val = os.environ.get("INSTAGRAM_USERNAMES", "")
        if env_val:
            instagram_usernames = [
                u.strip()
                for u in env_val.replace("'", "").replace('"', "").strip("[]").split(",")
                if u.strip()
            ]
        else:
            instagram_usernames = []

    return {
        "apify_api_key": apify_key,
        "google_maps_terms": maps_terms,
        "google_maps_location": maps_location,
        "foodpanda_url": foodpanda_url,
        "foodpanda_keyword": foodpanda_keyword,
        "instagram_usernames": instagram_usernames,
    }


def run_pipeline(store_config: dict | None = None) -> list[dict]:
    cfg = _load_config(store_config)
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
        return []

    return normalizer.normalize(all_raw)
