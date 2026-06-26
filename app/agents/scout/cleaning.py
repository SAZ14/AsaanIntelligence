from __future__ import annotations
import hashlib
import logging
import re
import unicodedata
from typing import Optional

from bs4 import BeautifulSoup

from app.agents.scout.schemas import FindingSchema

logger = logging.getLogger(__name__)

FOOD_KEYWORDS = {
    "cake", "ice cream", "dessert", "brownie", "bakery", "cookie", "chocolate",
    "flavor", "flavour", "menu", "offer", "deal", "discount", "sale", "price",
    "launch", "new", "season", "eid", "ramadan", "summer", "winter", "sweet",
    "candy", "milkshake", "sundae", "waffle", "cheesecake", "muffin", "pastry",
    "opening", "branch", "rating", "review", "stars", "pkr", "rs.", "rs ",
    "order", "delivery", "taste", "promo", "bundle",
}

VALID_PLATFORMS = {"instagram", "website", "google_maps", "web_search", "news"}
VALID_UPDATE_TYPES = {
    "new_product", "discount", "menu_change", "campaign",
    "review_trend", "event", "branch_update", "post",
}

MIN_CHARS = 30
MAX_CHARS = 1000
HASHTAG_ONLY_RE = re.compile(r"^(\s*#\w+\s*)+$")


def _strip_html(text: str) -> str:
    try:
        return BeautifulSoup(text, "html.parser").get_text(separator=" ")
    except Exception:
        return re.sub(r"<[^>]+>", " ", text)


def _normalize_unicode(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _remove_emoji_noise(text: str) -> str:
    # Remove standalone emoji clusters that carry no textual content
    cleaned = re.sub(
        r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
        r"\U0001F680-\U0001F6FF\U0001F700-\U0001F77F"
        r"\U0001F780-\U0001F7FF\U0001F800-\U0001F8FF"
        r"\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F"
        r"\U0001FA70-\U0001FAFF\U00002702-\U000027B0"
        r"\U000024C2-\U0001F251]+",
        " ",
        text,
    )
    return _collapse_whitespace(cleaned)


def clean_text(text: str) -> str:
    text = _strip_html(text)
    text = _normalize_unicode(text)
    text = _collapse_whitespace(text)
    text = text[:MAX_CHARS]
    return text


def _is_noise(text: str) -> bool:
    if len(text) < MIN_CHARS:
        return True
    if HASHTAG_ONLY_RE.match(text):
        return True
    lower = text.lower()
    if not any(kw in lower for kw in FOOD_KEYWORDS):
        return True
    return False


def _make_hash(competitor_name: str, text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha1(f"{competitor_name}||{normalized}".encode()).hexdigest()


def _coerce_engagement(eng) -> Optional[dict]:
    if eng is None:
        return None
    if not isinstance(eng, dict):
        return None
    out = {}
    for k, v in eng.items():
        try:
            out[k] = int(v) if v is not None else 0
        except (TypeError, ValueError):
            out[k] = 0
    return out


def _normalize_platform(platform: str) -> str:
    p = platform.lower().strip()
    if p in VALID_PLATFORMS:
        return p
    return "web_search"


def _normalize_update_type(ut: str) -> str:
    u = ut.lower().strip()
    if u in VALID_UPDATE_TYPES:
        return u
    return "post"


def clean_findings(
    findings: list[FindingSchema],
    seen_hashes: Optional[set] = None,
) -> list[FindingSchema]:
    if seen_hashes is None:
        seen_hashes = set()

    cleaned: list[FindingSchema] = []
    for f in findings:
        try:
            text = clean_text(f.content_text or "")
            if _is_noise(text):
                logger.debug("Dropping noisy finding from %s: %r", f.competitor_name, text[:60])
                continue

            content_hash = _make_hash(f.competitor_name, text)
            if content_hash in seen_hashes:
                logger.debug("Dropping duplicate from %s", f.competitor_name)
                continue
            seen_hashes.add(content_hash)

            cleaned.append(FindingSchema(
                competitor_name=f.competitor_name,
                source_platform=_normalize_platform(f.source_platform),
                update_type=_normalize_update_type(f.update_type),
                content_text=text,
                rating=f.rating,
                post_date=f.post_date,          # None is fine
                source_url=f.source_url,
                image_url=f.image_url,
                engagement=_coerce_engagement(f.engagement),
                ai_summary=f.ai_summary,
                relevance_score=f.relevance_score,
                content_hash=content_hash,
            ))
        except Exception as exc:
            logger.warning("Error cleaning finding: %s", exc)
            continue

    # Sort: known dates first (nulls last), then by relevance_score desc
    cleaned.sort(key=lambda x: (x.post_date is None, -(x.relevance_score or 0)))
    return cleaned

