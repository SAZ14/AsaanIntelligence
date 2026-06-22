from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from apify_client import ApifyClient


def _sha256(source: str, text: str) -> str:
    return hashlib.sha256(f"{source}:{text}".encode()).hexdigest()


def fetch_reviews(api_key: str, url: str, keyword: str = "") -> list[dict]:
    client = ApifyClient(api_key)

    run_input = {
        "url": url,
        "results_wanted": 20,
        "max_pages": 3,
    }
    if keyword:
        run_input["keyword"] = keyword

    run = client.actor("shahidirfan/food-panda-scraper").call(run_input=run_input)
    if not run:
        return []

    collected_at = datetime.now(timezone.utc).isoformat()
    all_reviews: list[dict] = []

    for item in client.dataset(run.default_dataset_id).iterate_items():
        name = item.get("name", "FoodPanda")
        source = f"FoodPanda - {name}"
        rating = item.get("rating")
        review_date = ""

        review_sample = item.get("review_sample") or []
        for sample in review_sample:
            if isinstance(sample, dict):
                text = (sample.get("text") or "").strip()
            else:
                text = str(sample).strip()
            if not text:
                continue
            all_reviews.append({
                "source": source,
                "text": text,
                "rating": rating,
                "review_date": sample.get("date", "") if isinstance(sample, dict) else "",
                "url": item.get("url", ""),
                "author": sample.get("author", "") if isinstance(sample, dict) else "",
                "collected_at": collected_at,
                "hash": _sha256(source, text),
            })

    return all_reviews
