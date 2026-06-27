from __future__ import annotations
import hashlib
import logging
from urllib.parse import quote_plus

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.agents.scout.config import APIFY_TOKEN, APIFY_MAPS_ACTOR, APIFY_REVIEWS_PER_COMPETITOR
from app.agents.scout.schemas import FindingSchema

logger = logging.getLogger(__name__)


def _make_hash(competitor_name: str, text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha1(f"{competitor_name}||{normalized}".encode()).hexdigest()


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=2, min=5, max=30),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _run_reviews_actor(search_url: str, max_reviews: int) -> list[dict]:
    from apify_client import ApifyClient
    client = ApifyClient(APIFY_TOKEN)
    run = client.actor(APIFY_MAPS_ACTOR).call(run_input={
        "startUrls": [{"url": search_url}],
        "maxReviews": max_reviews,
        "reviewsSort": "newest",
        "language": "en",
    })
    return list(client.dataset(run["defaultDatasetId"]).iterate_items())


def fetch_reviews(competitor: dict) -> list[FindingSchema]:
    """Fetch 20+ Google Maps reviews per competitor via Apify."""
    if not APIFY_TOKEN:
        logger.warning("Google Maps reviews disabled — no APIFY_TOKEN")
        return []

    name = competitor["name"]
    search_url = (
        f"https://www.google.com/maps/search/{quote_plus(name + ' Islamabad')}/"
    )

    try:
        items = _run_reviews_actor(search_url, APIFY_REVIEWS_PER_COMPETITOR)
    except Exception as exc:
        logger.error("Apify Google Maps reviews actor failed for %r: %s", name, exc)
        return []

    findings: list[FindingSchema] = []
    for item in items:
        try:
            review_text = (
                item.get("text")
                or item.get("textTranslated")
                or item.get("reviewText")
                or ""
            )
            if not review_text or len(review_text.strip()) < 15:
                continue

            rating = item.get("stars") or item.get("rating") or item.get("reviewRating")
            author = (
                item.get("name")
                or item.get("reviewerName")
                or item.get("authorName")
                or ""
            )
            published = (
                item.get("publishAt")
                or item.get("publishedAtDate")
                or item.get("relativePublishTimeDescription")
                or ""
            )

            text = f'"{review_text.strip()}"'
            if author:
                text += f" — {author}"
            if published:
                text = f"[{published}] {text}"
            text = text[:1000]

            findings.append(FindingSchema(
                competitor_name=name,
                source_platform="google_maps",
                update_type="review_trend",
                content_text=text,
                rating=float(rating) if rating else None,
                content_hash=_make_hash(name, text),
            ))
        except Exception as exc:
            logger.warning("Skipping review item: %s", exc)

    return findings
