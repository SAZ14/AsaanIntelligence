from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from apify_client import ApifyClient


def _sha256(source: str, text: str) -> str:
    return hashlib.sha256(f"{source}:{text}".encode()).hexdigest()


def _fetch_posts(client: ApifyClient, usernames: list[str]) -> list[dict]:
    urls = list(dict.fromkeys(f"https://www.instagram.com/{u}/" for u in usernames))
    run = client.actor("apify/instagram-scraper").call(run_input={
        "directUrls": urls,
        "resultsType": "posts",
        "resultsLimit": 10,
    })
    if not run:
        return []
    dataset_id = run["defaultDatasetId"] if isinstance(run, dict) else run.default_dataset_id
    return list(client.dataset(dataset_id).iterate_items())


def _fetch_comments(client: ApifyClient, post_urls: list[str]) -> list[dict]:
    if not post_urls:
        return []
    unique_urls = list(dict.fromkeys(u for u in post_urls if u))[:5]
    if not unique_urls:
        return []
    run = client.actor("apify/instagram-scraper").call(run_input={
        "directUrls": unique_urls,
        "resultsType": "comments",
        "resultsLimit": 50,
    })
    if not run:
        return []
    dataset_id = run["defaultDatasetId"] if isinstance(run, dict) else run.default_dataset_id
    return list(client.dataset(dataset_id).iterate_items())


def fetch_reviews(api_key: str, usernames: list[str]) -> list[dict]:
    """Return customer-generated Instagram content for reputation checking.

    Only comments qualify -- they're actual customer reactions. Post
    captions are the business's OWN copy, not customer feedback; treating
    them as "reviews" made no sense (what would "sentiment toward the
    business" of the business's own marketing caption even mean?), and
    with the old rating=0-defaults-to-negative classifier bug, a caption
    older than 3 days could even surface as a "pending negative review"
    needing a reply -- to your own post. Posts are still fetched here
    purely to discover which URLs to pull comments from.
    """
    client = ApifyClient(api_key)
    collected_at = datetime.now(timezone.utc).isoformat()

    posts = _fetch_posts(client, usernames)
    if not posts:
        return []

    post_urls = [p.get("url", "") for p in posts if p.get("url")]
    comments = _fetch_comments(client, post_urls)

    all_items: list[dict] = []

    seen: set[str] = set()
    for c in comments:
        text = (c.get("text") or "").strip()
        if not text:
            continue
        source = f"Instagram - {c.get('parentPostUrl', usernames[0])} comment"
        h = _sha256(source, text)
        if h in seen:
            continue
        seen.add(h)
        all_items.append({
            "source": source,
            "text": text,
            "rating": None,
            "review_date": c.get("timestamp", ""),
            "url": c.get("parentPostUrl", ""),
            "author": c.get("ownerUsername", "anonymous"),
            "collected_at": collected_at,
            "hash": h,
        })

    return all_items
