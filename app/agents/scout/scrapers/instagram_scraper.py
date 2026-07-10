from __future__ import annotations
import hashlib
import logging
from datetime import datetime
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from app.agents.scout.config import APIFY_TOKEN, APIFY_IG_ACTOR, IG_POSTS_PER_PROFILE
from app.agents.scout.schemas import FindingSchema
from app.core.apify_errors import ApifyQuotaExceeded, is_quota_error, should_retry_apify_call

logger = logging.getLogger(__name__)


def _make_hash(competitor_name: str, text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha1(f"{competitor_name}||{normalized}".encode()).hexdigest()


def _infer_update_type(caption: str) -> str:
    lower = caption.lower()
    if any(w in lower for w in ["off", "deal", "sale", "discount", "bogo", "free", "%"]):
        return "discount"
    seasonal = ["eid", "ramadan", "summer", "winter", "valentine", "christmas", "holiday", "fest"]
    if any(w in lower for w in seasonal):
        return "campaign"
    if any(w in lower for w in ["new", "introducing", "launch", "now available", "just dropped", "try our"]):
        return "new_product"
    return "post"


def _parse_timestamp(ts) -> Optional[datetime]:
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts
    try:
        # ISO format
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        pass
    try:
        return datetime.utcfromtimestamp(int(ts))
    except Exception:
        return None


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    retry=retry_if_exception(should_retry_apify_call),
    reraise=True,
)
def _run_actor(usernames: list[str], hashtags: list[str], limit: int) -> list[dict]:
    from apify_client import ApifyClient
    client = ApifyClient(APIFY_TOKEN)
    run_input: dict = {"resultsLimit": limit}
    if usernames:
        run_input["username"] = usernames
    if hashtags:
        run_input["hashtags"] = hashtags
    run = client.actor(APIFY_IG_ACTOR).call(run_input=run_input)
    dataset_id = run["defaultDatasetId"] if isinstance(run, dict) else run.default_dataset_id
    items = list(client.dataset(dataset_id).iterate_items())
    return items


def fetch_recent_posts(handles: list[str], limit: int = IG_POSTS_PER_PROFILE) -> list[FindingSchema]:
    if not APIFY_TOKEN:
        logger.warning("Instagram scraper disabled — APIFY_TOKEN not set")
        return []

    if not handles:
        return []

    # Split into regular handles and hashtag entries (stored as "#hashtag" in DB)
    usernames = [h for h in handles if not h.startswith("#")]
    hashtags = [h.lstrip("#") for h in handles if h.startswith("#")]

    try:
        items = _run_actor(usernames, hashtags, limit)
    except Exception as exc:
        logger.error("Apify Instagram actor failed: %s", exc)
        if is_quota_error(exc):
            raise ApifyQuotaExceeded(str(exc)) from exc
        return []

    findings: list[FindingSchema] = []
    for item in items:
        try:
            caption = item.get("caption") or item.get("text") or ""
            if not caption or not caption.strip():
                continue

            # Hashtag-sourced items carry the hashtag context; regular items carry ownerUsername
            hashtag_ctx = item.get("hashtag") or item.get("topHashtag")
            if hashtag_ctx:
                competitor_name = f"Global Trend: #{hashtag_ctx}"
            else:
                owner = (
                    item.get("ownerUsername")
                    or item.get("username")
                    or item.get("ownerId", "")
                )
                competitor_name = _handle_to_name(str(owner), usernames)

            text = caption[:1000]
            likes = _safe_int(item.get("likesCount") or item.get("likes"))
            comments = _safe_int(item.get("commentsCount") or item.get("comments"))

            post_url = item.get("url") or (
                item.get("shortCode") and f"https://instagram.com/p/{item['shortCode']}"
            )
            image_url = item.get("displayUrl") or item.get("thumbnailUrl")

            findings.append(FindingSchema(
                competitor_name=competitor_name,
                source_platform="instagram",
                update_type=_infer_update_type(text),
                content_text=text,
                post_date=_parse_timestamp(item.get("timestamp") or item.get("takenAt")),
                source_url=post_url,
                image_url=image_url,
                engagement={"likes": likes, "comments": comments},
                content_hash=_make_hash(competitor_name, text),
            ))
        except Exception as exc:
            logger.warning("Skipping IG item due to error: %s", exc)
            continue

    return findings


def _handle_to_name(owner: str, handles: list[str]) -> str:
    owner_lower = owner.lower()
    for h in handles:
        if h.lower() == owner_lower:
            return h  # caller can remap if they want the display name
    return owner


def _safe_int(val) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0

