from __future__ import annotations
import logging
import re
from typing import Optional

from app.agents.scout.config import COMPETITORS, MAX_NEW_COMPETITORS, FIRECRAWL_API_KEY, GOOGLE_PLACES_API_KEY
from app.core.db import Competitor, SessionLocal

logger = logging.getLogger(__name__)

_IG_URL_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]+)/?")


def _extract_ig_handle(url_or_text: str) -> Optional[str]:
    m = _IG_URL_RE.search(url_or_text)
    if m:
        handle = m.group(1)
        if handle.lower() not in ("p", "reel", "explore", "stories", "tv"):
            return handle
    return None


def _resolve_handle_via_firecrawl(name: str) -> Optional[str]:
    if not FIRECRAWL_API_KEY:
        return None
    try:
        from app.agents.scout.scrapers.firecrawl_scraper import search
        results = search(f"{name} Islamabad instagram", limit=5)
        for r in results:
            handle = _extract_ig_handle(r.get("url", "") + " " + r.get("description", ""))
            if handle:
                return handle
    except Exception as exc:
        logger.warning("Firecrawl handle resolution failed for %r: %s", name, exc)
    return None


def _resolve_place_id(name: str) -> Optional[str]:
    if not GOOGLE_PLACES_API_KEY:
        return None
    try:
        from app.agents.scout.scrapers.places_scraper import _text_search
        places = _text_search(f"{name} Islamabad")
        if places:
            return places[0].get("id")
    except Exception as exc:
        logger.warning("Places resolve failed for %r: %s", name, exc)
    return None


def confirm_seed_competitors(store_id: int) -> None:
    """Resolve missing handles/place_ids for seed competitors of a store."""
    with SessionLocal() as db:
        rows = db.query(Competitor).filter(
            Competitor.store_id == store_id,
            Competitor.source == "seed",
        ).all()
        for row in rows:
            updated = False
            if row.instagram_handle is None:
                handle = _resolve_handle_via_firecrawl(row.name)
                if handle:
                    row.instagram_handle = handle
                    updated = True
                    logger.info("Resolved IG handle for %r: %s", row.name, handle)
            if row.place_id is None:
                place_id = _resolve_place_id(row.name)
                if place_id:
                    row.place_id = place_id
                    updated = True
                    logger.info("Resolved Place ID for %r: %s", row.name, place_id)
            if updated:
                db.commit()


def discover_new_competitors(store_id: int) -> None:
    """Search for new competitors not already in the store's DB. Add top results."""
    if not FIRECRAWL_API_KEY:
        logger.info("Competitor discovery skipped — Firecrawl not configured")
        return

    from app.agents.scout.scrapers.firecrawl_scraper import search

    discovery_queries = [
        "best dessert cafe Islamabad 2026",
        "new ice cream shop Islamabad",
        "top bakeries Islamabad",
        "popular dessert shop Islamabad instagram",
    ]

    candidates: list[str] = []
    for q in discovery_queries:
        results = search(q, limit=5)
        for r in results:
            title = r.get("title", "")
            desc = r.get("description", "")
            name = _extract_business_name(title or desc)
            if name and len(name) > 3:
                candidates.append(name)

    if not candidates:
        return

    with SessionLocal() as db:
        existing_names = {
            c.name.lower()
            for c in db.query(Competitor).filter(Competitor.store_id == store_id).all()
        }
        added = 0
        for name in candidates:
            if added >= MAX_NEW_COMPETITORS:
                break
            if name.lower() in existing_names:
                continue
            if _is_sugar_rush(name):
                continue
            db.add(Competitor(store_id=store_id, name=name, source="discovered"))
            existing_names.add(name.lower())
            added += 1
            logger.info("Discovered new competitor: %r", name)
        if added:
            db.commit()


_NOISE_STARTS = re.compile(
    r"^(best|top|good|great|amazing|popular|new|famous|find|explore|"
    r"try|visit|one of|some of|list of|here are|check out|where|what|"
    r"how|why|when|is|are|was|the best|a|an)\b",
    re.I,
)
_ENDS_PREPOSITION = re.compile(r"\b(in|at|of|the|a|an|and|or|for|to|from|with)$", re.I)


def _extract_business_name(text: str) -> Optional[str]:
    m = re.split(r"[|\-–—:@]", text)
    if not m:
        return None
    candidate = m[0].strip()
    candidate = re.sub(r"\s*(islamabad|pakistan|lahore|karachi)\s*$", "", candidate, flags=re.I).strip()
    if not (3 < len(candidate) < 50):
        return None
    if "?" in candidate:
        return None
    if _NOISE_STARTS.match(candidate):
        return None
    if _ENDS_PREPOSITION.search(candidate):
        return None
    words = candidate.split()
    if not any(w[0].isupper() for w in words if len(w) > 2):
        return None
    if len(words) > 5:
        return None
    return candidate


def _is_sugar_rush(name: str) -> bool:
    return "sugar rush" in name.lower()


def get_all_competitors(store_id: int) -> list[dict]:
    """Return all competitors for a store as dicts."""
    with SessionLocal() as db:
        rows = db.query(Competitor).filter(Competitor.store_id == store_id).all()
        return [
            {
                "name": r.name,
                "category": r.category,
                "instagram_handle": r.instagram_handle,
                "website": r.website,
                "place_id": r.place_id,
                "source": r.source,
            }
            for r in rows
        ]

