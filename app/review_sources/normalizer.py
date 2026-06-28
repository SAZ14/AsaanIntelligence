from __future__ import annotations

from datetime import datetime

from app.models.canonical import Review


def normalize(raw_reviews: list[dict]) -> list[dict]:
    for r in raw_reviews:
        r.setdefault("source", "unknown")
        r.setdefault("text", "")
        r.setdefault("rating", None)
        r.setdefault("review_date", "")
        r.setdefault("url", "")
        r.setdefault("author", "anonymous")
        r.setdefault("collected_at", datetime.utcnow().isoformat())
        r.setdefault("hash", "")
    return raw_reviews


def to_review_model(d: dict) -> Review:
    posted_at = d.get("review_date") or d.get("collected_at", "")
    if isinstance(posted_at, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S.%f",
                     "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                     "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                posted_at = datetime.strptime(posted_at, fmt)
                break
            except ValueError:
                continue
        else:
            posted_at = datetime.utcnow()

    rating = d.get("rating") or 0

    return Review(
        review_id=d.get("hash", ""),
        source=d.get("source", "unknown"),
        rating=int(rating) if rating else 0,
        posted_at=posted_at,
        reviewer_name=d.get("author", "anonymous"),
        text=d.get("text", ""),
    )
