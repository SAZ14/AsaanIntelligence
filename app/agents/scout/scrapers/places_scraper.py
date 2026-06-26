from __future__ import annotations
import hashlib
import logging
import os
from datetime import datetime
from typing import Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.agents.scout.config import GOOGLE_PLACES_API_KEY
from app.agents.scout.schemas import FindingSchema

logger = logging.getLogger(__name__)

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

FIELD_MASK = ",".join([
    "places.displayName",
    "places.formattedAddress",
    "places.rating",
    "places.userRatingCount",
    "places.reviews",
    "places.id",
])


def _make_hash(competitor_name: str, text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha1(f"{competitor_name}||{normalized}".encode()).hexdigest()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(requests.RequestException),
    reraise=True,
)
def _text_search(query: str) -> list[dict]:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    r = requests.post(PLACES_SEARCH_URL, headers=headers, json={"textQuery": query}, timeout=30)
    r.raise_for_status()
    return r.json().get("places", [])


def fetch_reviews_and_rating(competitor: dict) -> list[FindingSchema]:
    if not GOOGLE_PLACES_API_KEY:
        logger.warning("Google Places disabled — GOOGLE_PLACES_API_KEY not set")
        return []

    name = competitor["name"]
    query = f"{name} Islamabad"

    try:
        places = _text_search(query)
    except Exception as exc:
        logger.error("Google Places search failed for %r: %s", name, exc)
        return []

    if not places:
        logger.info("No Places results for %r", name)
        return []

    findings: list[FindingSchema] = []
    addresses_seen: set[str] = set()

    for place in places:
        display_name = place.get("displayName", {}).get("text", name)
        address = place.get("formattedAddress", "")
        rating = place.get("rating")
        review_count = place.get("userRatingCount")
        place_id = place.get("id", "")

        # Detect new branches — multiple distinct addresses = branch_update
        if address and address not in addresses_seen:
            addresses_seen.add(address)

        # Emit a rating summary finding for the first/top result
        if len(findings) == 0 and rating is not None:
            summary = (
                f"{display_name} has a Google Maps rating of {rating}/5 "
                f"({review_count or 'N/A'} reviews). Location: {address}."
            )
            findings.append(FindingSchema(
                competitor_name=name,
                source_platform="google_maps",
                update_type="review_trend",
                content_text=summary,
                rating=rating,
                engagement={"review_count": review_count},
                content_hash=_make_hash(name, summary),
            ))

        # Emit individual review findings
        for review in place.get("reviews", []):
            review_text = review.get("text", {}).get("text", "")
            if not review_text or len(review_text.strip()) < 20:
                continue
            review_rating = review.get("rating")
            relative_time = review.get("relativePublishTimeDescription", "")
            author = review.get("authorAttribution", {}).get("displayName", "")
            text = f'[{relative_time}] "{review_text}" — {author}'.strip()[:1000]
            findings.append(FindingSchema(
                competitor_name=name,
                source_platform="google_maps",
                update_type="review_trend",
                content_text=text,
                rating=float(review_rating) if review_rating else None,
                content_hash=_make_hash(name, text),
            ))

    # If multiple distinct addresses found for the same business, emit branch update
    if len(addresses_seen) > 1:
        branch_text = f"{name} has multiple Islamabad locations: {'; '.join(list(addresses_seen)[:5])}"
        findings.append(FindingSchema(
            competitor_name=name,
            source_platform="google_maps",
            update_type="branch_update",
            content_text=branch_text,
            content_hash=_make_hash(name, branch_text),
        ))

    return findings

