from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from apify_client import ApifyClient


def _sha256(source: str, text: str) -> str:
    return hashlib.sha256(f"{source}:{text}".encode()).hexdigest()


def _fetch_posts(client: ApifyClient, usernames: list[str]) -> list[dict]:
    urls = [f"https://www.instagram.com/{u}/" for u in usernames]
    run = client.actor("apify/instagram-scraper").call(run_input={
        "directUrls": urls,
        "resultsType": "posts",
        "resultsLimit": 10,
    })
    if not run:
        return []
    return list(client.dataset(run["defaultDatasetId"]).iterate_items())


def _fetch_comments(client: ApifyClient, post_urls: list[str]) -> list[dict]:
    if not post_urls:
        return []
    run = client.actor("apify/instagram-scraper").call(run_input={
        "directUrls": post_urls[:5],
        "resultsType": "comments",
        "resultsLimit": 50,
    })
    if not run:
        return []
    return list(client.dataset(run["defaultDatasetId"]).iterate_items())


def fetch_reviews(api_key: str, usernames: list[str]) -> list[dict]:
    client = ApifyClient(api_key)
    collected_at = datetime.now(timezone.utc).isoformat()

    posts = _fetch_posts(client, usernames)
    if not posts:
        return []

    post_urls = [p.get("url", "") for p in posts if p.get("url")]
    comments = _fetch_comments(client, post_urls)

    all_items: list[dict] = []

    for p in posts:
        caption = (p.get("caption") or "").strip()
        source = f"Instagram - {p.get('ownerUsername', usernames[0])}"
        if caption:
            all_items.append({
                "source": source,
                "text": caption,
                "rating": None,
                "review_date": p.get("timestamp", ""),
                "url": p.get("url", ""),
                "author": p.get("ownerUsername", ""),
                "collected_at": collected_at,
                "hash": _sha256(source + " post", caption),
            })

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
