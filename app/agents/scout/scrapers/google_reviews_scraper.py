from __future__ import annotations
import hashlib
import logging
from urllib.parse import quote_plus

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from app.agents.scout.config import APIFY_TOKEN, APIFY_MAPS_ACTOR, APIFY_REVIEWS_PER_COMPETITOR
from app.agents.scout.schemas import FindingSchema
from app.core.apify_errors import ApifyQuotaExceeded, is_quota_error, should_retry_apify_call

logger = logging.getLogger(__name__)

_TOPIC_KEYWORDS: dict[str, list[str]] = {
    "price":    ["price", "cheap", "expensive", "value", "worth", "cost", "overpriced", "affordable", "pkr", "rs."],
    "quality":  ["taste", "quality", "fresh", "stale", "delicious", "flavour", "flavor", "soggy", "dry"],
    "service":  ["service", "staff", "rude", "friendly", "helpful", "attentive", "waiter", "cashier"],
    "wait":     ["wait", "waiting", "queue", "slow", "quick", "delivery", "time", "late"],
    "ambiance": ["ambiance", "atmosphere", "clean", "dirty", "cozy", "noisy", "parking", "seating"],
}

# Two-pass strategy: perception-shaping reviews + competitor pain points.
# reviewsSort accepts: newest | mostRelevant | highestRanking | lowestRanking
# (per compass/google-maps-reviews-scraper's input schema).
_REVIEW_STRATEGIES: list[tuple[str, int]] = [
    ("mostRelevant", max(1, APIFY_REVIEWS_PER_COMPETITOR * 2 // 3)),
    ("lowestRanking", max(1, APIFY_REVIEWS_PER_COMPETITOR // 3)),
]


def _make_hash(competitor_name: str, text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha1(f"{competitor_name}||{normalized}".encode()).hexdigest()


def _classify_update_type(rating, text: str) -> str:
    if rating is not None:
        r = float(rating)
        if r >= 4:
            return "competitor_strength"
        if r <= 2:
            return "competitor_weakness"
    lower = text.lower()
    neg = ["worst", "terrible", "awful", "horrible", "rude", "never again", "waste", "disgusting"]
    pos = ["best", "amazing", "excellent", "love", "fantastic", "highly recommend", "must try"]
    if any(w in lower for w in neg):
        return "competitor_weakness"
    if any(w in lower for w in pos):
        return "competitor_strength"
    return "review_trend"


def _extract_topics(text: str) -> list[str]:
    lower = text.lower()
    return [t for t, kws in _TOPIC_KEYWORDS.items() if any(kw in lower for kw in kws)]


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=2, min=5, max=30),
    retry=retry_if_exception(should_retry_apify_call),
    reraise=True,
)
def _run_actor(search_url: str, sort: str, max_reviews: int) -> list[dict]:
    from apify_client import ApifyClient
    client = ApifyClient(APIFY_TOKEN)
    run = client.actor(APIFY_MAPS_ACTOR).call(run_input={
        "startUrls": [{"url": search_url}],
        "maxReviews": max_reviews,
        "reviewsSort": sort,
        "language": "en",
    })
    dataset_id = run["defaultDatasetId"] if isinstance(run, dict) else run.default_dataset_id
    return list(client.dataset(dataset_id).iterate_items())


def fetch_reviews(competitor: dict) -> list[FindingSchema]:
    """Two-pass Google Maps review fetch via Apify.

    Pass 1 (mostRelevant): reviews shaping customer perception.
    Pass 2 (lowestRanking): competitor pain points = Sugar Rush opportunities.
    Results are deduplicated, sentiment-classified (strength/weakness/trend),
    and tagged with topics (price, quality, service, wait, ambiance).
    """
    if not APIFY_TOKEN:
        logger.warning("Google Maps reviews disabled — no APIFY_TOKEN")
        return []

    name = competitor["name"]
    search_url = f"https://www.google.com/maps/search/{quote_plus(name + ' Islamabad')}/"

    raw: list[tuple[dict, str]] = []
    for sort_key, max_reviews in _REVIEW_STRATEGIES:
        try:
            items = _run_actor(search_url, sort_key, max_reviews)
            label = "most_relevant" if sort_key == "mostRelevant" else "lowest_rated"
            raw.extend((item, label) for item in items)
            logger.info("Maps %s for %r: %d items", sort_key, name, len(items))
        except Exception as exc:
            logger.error("Apify Maps actor (%s) failed for %r: %s", sort_key, name, exc)
            if is_quota_error(exc):
                raise ApifyQuotaExceeded(str(exc)) from exc

    seen: set[str] = set()
    findings: list[FindingSchema] = []

    for item, strategy in raw:
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
                item.get("name") or item.get("reviewerName") or item.get("authorName") or ""
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

            h = _make_hash(name, text)
            if h in seen:
                continue
            seen.add(h)

            topics = _extract_topics(review_text)
            update_type = _classify_update_type(rating, review_text)

            findings.append(FindingSchema(
                competitor_name=name,
                source_platform="google_maps",
                update_type=update_type,
                content_text=text,
                rating=float(rating) if rating else None,
                engagement={"strategy": strategy, "topics": topics},
                content_hash=h,
            ))
        except Exception as exc:
            logger.warning("Skipping review item: %s", exc)

    logger.info(
        "Google Maps reviews for %r: %d unique (%d strength / %d weakness / %d trend)",
        name, len(findings),
        sum(1 for f in findings if f.update_type == "competitor_strength"),
        sum(1 for f in findings if f.update_type == "competitor_weakness"),
        sum(1 for f in findings if f.update_type == "review_trend"),
    )
    return findings
