from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from apify_client import ApifyClient

logger = logging.getLogger(__name__)


def _sha256(source: str, text: str) -> str:
    return hashlib.sha256(f"{source}:{text}".encode()).hexdigest()


def fetch_reviews(
    api_key: str, search_terms: list[str], location: str, business_name: str | None = None,
) -> list[dict]:
    client = ApifyClient(api_key)

    run_input = {
        "searchStringsArray": search_terms,
        "locationQuery": location,
        # 1, not 5 -- confirmed live: with 5, Google's search for an
        # ambiguous/loosely-matching term returns other nearby places
        # alongside (or instead of) the actual business, and the actor
        # crawls all of them with no validation that the result is even
        # the right business. Real production data: 52+ completely
        # unrelated places (a hotel, a Thai restaurant, biryani spots)
        # ended up stored as this store's own reviews. Only the single
        # best match per search term now, plus the business_name filter
        # below as a second line of defense.
        "maxCrawledPlacesPerSearch": 1,
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
    name_filter = (business_name or "").strip().lower()

    dataset_id = run["defaultDatasetId"] if isinstance(run, dict) else run.default_dataset_id
    for item in client.dataset(dataset_id).iterate_items():
        title = item.get("title", "Google Maps")

        # Defense in depth: even maxCrawledPlacesPerSearch=1 can return the
        # wrong top result for an ambiguous search term. Discard anything
        # whose title doesn't actually contain the store's own name rather
        # than trusting Google's match silently.
        if name_filter and name_filter not in title.lower():
            logger.warning(
                "google_maps.fetch_reviews: discarding unrelated place %r "
                "(expected business_name containing %r)", title, business_name,
            )
            continue

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
