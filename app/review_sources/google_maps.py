from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from apify_client import ApifyClient


def _sha256(source: str, text: str) -> str:
    return hashlib.sha256(f"{source}:{text}".encode()).hexdigest()


def fetch_reviews(api_key: str, search_terms: list[str], location: str) -> list[dict]:
    client = ApifyClient(api_key)

    run_input = {
        "searchStringsArray": search_terms,
        "locationQuery": location,
        "maxCrawledPlacesPerSearch": 5,
        "maxReviews": 50,
        "scrapePlaceDetailPage": True,
        "language": "en",
        "reviewsSort": "newest",
    }

    run = client.actor("compass/crawler-google-places").call(run_input=run_input)
    if not run:
        return []

    collected_at = datetime.now(timezone.utc).isoformat()
    all_reviews: list[dict] = []

    dataset_id = run["defaultDatasetId"] if isinstance(run, dict) else run.default_dataset_id
    for item in client.dataset(dataset_id).iterate_items():
        title = item.get("title", "Google Maps")
        source = f"Google Maps - {title}"

        for r in item.get("reviews", []):
            text = (r.get("text") or "").strip()
            if not text:
                continue
            all_reviews.append({
                "source": source,
                "text": text,
                "rating": r.get("stars"),
                "review_date": r.get("publishedAtDate") or r.get("publishAt", ""),
                "url": r.get("reviewUrl", item.get("url", "")),
                "author": r.get("name", "anonymous"),
                "collected_at": collected_at,
                "hash": _sha256(source, text),
            })

    return all_reviews
