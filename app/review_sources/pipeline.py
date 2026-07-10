from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.review_sources import normalizer
from app.review_sources.google_maps import fetch_reviews as fetch_maps
from app.review_sources.instagram import fetch_reviews as fetch_instagram

log = logging.getLogger("review_sources.pipeline")


def _load_config_from_db(store_id: int) -> dict:
    """Fetch per-store scraping config from ReputationConfig table."""
    from app.core.db import SessionLocal, ReputationConfig, Store

    with SessionLocal() as db:
        rc = db.query(ReputationConfig).filter(ReputationConfig.store_id == store_id).first()
        store = db.query(Store).filter(Store.id == store_id).first()
        store_name = store.name if store else None

    if rc is None:
        log.warning("No ReputationConfig for store %d — reviews cannot be scraped", store_id)
        return {
            "apify_api_key": os.environ.get("APIFY_TOKEN", ""),
            "google_maps_terms": [],
            "google_maps_location": "",
            "instagram_usernames": [],
            "store_name": store_name,
        }

    return {
        "apify_api_key": os.environ.get("APIFY_TOKEN", ""),
        "google_maps_terms": rc.google_maps_terms or [],
        "google_maps_location": rc.google_maps_location or "",
        "instagram_usernames": rc.instagram_usernames or [],
        "store_name": store_name,
    }


def run_pipeline(store_id: int | None = None) -> tuple[list[dict], list[str], list[str], bool]:
    """Run all review scrapers for one store in parallel.

    If store_id is provided, config is loaded from the ReputationConfig DB table.
    If omitted, returns no reviews and no sources (all per-store config must
    live in the DB).

    Returns (reviews, sources_ok, sources_failed, quota_exceeded) -- callers
    need sources_ok/sources_failed to record what actually happened on this
    run rather than assuming success (previously hardcoded to ["pipeline"],
    [] regardless of which individual scrapers succeeded or failed).
    quota_exceeded is True if any source failed specifically due to Apify's
    account-level usage limit, as opposed to a routine per-item failure --
    callers use this to avoid overwriting good cached data with an empty
    result during an outage (see app.core.apify_guard).
    """
    if store_id is None:
        log.warning("run_pipeline called without store_id — no reviews scraped")
        return [], [], [], False

    cfg = _load_config_from_db(store_id)
    key = cfg["apify_api_key"]

    # Build task map: source_name → callable
    # FoodPanda scraping was removed: across every historical run for every
    # store, it produced zero usable reviews (no official reviews API exists,
    # and every Apify actor tried -- including the one previously wired in
    # here -- failed to reliably extract real review text).
    tasks: dict[str, object] = {}
    if key and cfg["google_maps_terms"]:
        tasks["google_maps"] = lambda: fetch_maps(
            key, cfg["google_maps_terms"], cfg["google_maps_location"], cfg["store_name"],
        )
    if key and cfg["instagram_usernames"]:
        tasks["instagram"] = lambda: fetch_instagram(key, cfg["instagram_usernames"])

    if not tasks:
        log.info("No scrape sources configured for store %d.", store_id)
        return [], [], [], False

    from app.core.apify_errors import is_quota_error

    all_raw: list[dict] = []
    sources_ok: list[str] = []
    sources_failed: list[str] = []
    quota_exceeded = False
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        future_to_name = {executor.submit(fn): name for name, fn in tasks.items()}
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                reviews = future.result()
                log.info("%s: %d reviews", name, len(reviews))
                all_raw.extend(reviews)
                sources_ok.append(name)
            except Exception as exc:
                log.error("%s scrape failed: %s", name, exc)
                sources_failed.append(name)
                if is_quota_error(exc):
                    quota_exceeded = True

    if not all_raw:
        log.info("No reviews found for store %d.", store_id)
        return [], sources_ok, sources_failed, quota_exceeded

    return normalizer.normalize(all_raw), sources_ok, sources_failed, quota_exceeded
