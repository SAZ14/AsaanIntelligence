from __future__ import annotations
import logging
import re
from typing import Optional

from app.agents.scout.config import COMPETITORS, MAX_NEW_COMPETITORS, FIRECRAWL_API_KEY, GOOGLE_PLACES_API_KEY
from app.core.db import Competitor, SessionLocal, Store

logger = logging.getLogger(__name__)

_IG_URL_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]+)/?")


def _store_context(store_id: int) -> tuple[str, str]:
    """Return (city, brand_name) for a store — used in discovery queries and exclusion."""
    with SessionLocal() as db:
        store = db.query(Store).filter(Store.id == store_id).first()
        if not store:
            return "city", "this restaurant"
        # location field holds the human-readable location string, e.g. "Kohsar Market, F-6, Islamabad"
        city = "Islamabad"
        if store.location:
            # take the last comma-separated token as city hint
            parts = [p.strip() for p in store.location.split(",")]
            if parts:
                city = parts[-1]
        return city, store.name


def _extract_ig_handle(url_or_text: str) -> Optional[str]:
    m = _IG_URL_RE.search(url_or_text)
    if m:
        handle = m.group(1)
        if handle.lower() not in ("p", "reel", "explore", "stories", "tv"):
            return handle
    return None


def _resolve_handle_via_firecrawl(name: str, city: str) -> Optional[str]:
    if not FIRECRAWL_API_KEY:
        return None
    try:
        from app.agents.scout.scrapers.firecrawl_scraper import search
        results = search(f"{name} {city} instagram", limit=5)
        for r in results:
            handle = _extract_ig_handle(r.get("url", "") + " " + r.get("description", ""))
            if handle:
                return handle
    except Exception as exc:
        logger.warning("Firecrawl handle resolution failed for %r: %s", name, exc)
    return None


def _resolve_place_id(name: str, city: str) -> Optional[str]:
    if not GOOGLE_PLACES_API_KEY:
        return None
    try:
        from app.agents.scout.scrapers.places_scraper import _text_search
        places = _text_search(f"{name} {city}")
        if places:
            return places[0].get("id")
    except Exception as exc:
        logger.warning("Places resolve failed for %r: %s", name, exc)
    return None


def confirm_seed_competitors(store_id: int) -> None:
    """Resolve missing handles/place_ids for seed competitors of a store."""
    city, _ = _store_context(store_id)
    with SessionLocal() as db:
        rows = db.query(Competitor).filter(
            Competitor.store_id == store_id,
            Competitor.source == "seed",
        ).all()
        for row in rows:
            updated = False
            if row.instagram_handle is None:
                handle = _resolve_handle_via_firecrawl(row.name, city)
                if handle:
                    row.instagram_handle = handle
                    updated = True
                    logger.info("Resolved IG handle for %r: %s", row.name, handle)
            if row.place_id is None:
                place_id = _resolve_place_id(row.name, city)
                if place_id:
                    row.place_id = place_id
                    updated = True
                    logger.info("Resolved Place ID for %r: %s", row.name, place_id)
            if updated:
                db.commit()


def discover_new_competitors(store_id: int) -> None:
    """Search for new competitors not already in the store's DB."""
    if not FIRECRAWL_API_KEY:
        logger.info("Competitor discovery skipped — Firecrawl not configured")
        return

    city, brand_name = _store_context(store_id)
    category = _store_category(store_id)

    from app.agents.scout.scrapers.firecrawl_scraper import search

    discovery_queries = [
        f"best {category} {city} 2026",
        f"new {category} {city}",
        f"top cafes {city}",
        f"popular {category} {city} instagram",
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
            if _is_own_brand(name, brand_name):
                continue
            db.add(Competitor(store_id=store_id, name=name, source="discovered"))
            existing_names.add(name.lower())
            added += 1
            logger.info("Discovered new competitor: %r (store=%d)", name, store_id)
        if added:
            db.commit()


def _store_category(store_id: int) -> str:
    with SessionLocal() as db:
        store = db.query(Store).filter(Store.id == store_id).first()
        if store and store.category:
            return store.category
        return "cafe"


def _is_own_brand(candidate: str, brand_name: str) -> bool:
    """Return True if candidate looks like the store's own brand (exclude self)."""
    c = candidate.lower().strip()
    b = brand_name.lower().strip()
    # exact match or candidate is a substring of brand name and vice versa
    return c == b or c in b or b in c


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
    # strip city suffixes
    candidate = re.sub(
        r"\s*(islamabad|lahore|karachi|pakistan|city)\s*$", "", candidate, flags=re.I
    ).strip()
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


def seed_competitors_for_store(store_id: int) -> int:
    """Seed the config COMPETITORS list into the DB for a store. Skips duplicates."""
    added = 0
    with SessionLocal() as db:
        existing = {
            c.name.lower()
            for c in db.query(Competitor).filter(Competitor.store_id == store_id).all()
        }
        for comp in COMPETITORS:
            if comp["name"].lower() in existing:
                continue
            db.add(Competitor(
                store_id=store_id,
                name=comp["name"],
                category=comp.get("category"),
                instagram_handle=comp.get("instagram_handle"),
                website=comp.get("website"),
                source="seed",
            ))
            existing.add(comp["name"].lower())
            added += 1
        if added:
            db.commit()
    return added
