from __future__ import annotations
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from app.agents.scout.config import (
    COMPETITORS, MAX_NEW_COMPETITORS, APIFY_TOKEN,
    PRUNE_MIN_AGE_DAYS, PRUNE_MIN_FINDINGS,
)
from app.core.db import Competitor, Finding, SessionLocal, Store, StoreLocation

logger = logging.getLogger(__name__)

_IG_URL_RE = re.compile(r"instagram\.com/([A-Za-z0-9_.]+)/?")


def _store_context(store_id: int) -> tuple[str, str]:
    """Return (city, brand_name) for a store."""
    with SessionLocal() as db:
        store = db.query(Store).filter(Store.id == store_id).first()
        if not store:
            return "Islamabad", "this restaurant"
        city = "Islamabad"
        if store.location:
            parts = [p.strip() for p in store.location.split(",")]
            if parts:
                city = parts[-1]
        return city, store.name


def _store_cities(store_id: int) -> set[str]:
    """Return the set of cities the store operates in (from StoreLocation rows)."""
    with SessionLocal() as db:
        locs = db.query(StoreLocation).filter(StoreLocation.store_id == store_id).all()
        cities = {l.city.strip().lower() for l in locs if l.city}
    if not cities:
        city, _ = _store_context(store_id)
        cities = {city.strip().lower()}
    return cities


def _extract_ig_handle(url_or_text: str) -> Optional[str]:
    m = _IG_URL_RE.search(url_or_text)
    if m:
        handle = m.group(1)
        if handle.lower() not in ("p", "reel", "explore", "stories", "tv"):
            return handle
    return None


def _resolve_handle_via_web_search(name: str, city: str) -> Optional[str]:
    if not APIFY_TOKEN:
        return None
    try:
        from app.agents.scout.scrapers.web_scraper import search
        results = search(f"{name} {city} instagram", limit=5)
        for r in results:
            handle = _extract_ig_handle(r.get("url", "") + " " + r.get("description", ""))
            if handle:
                return handle
    except Exception as exc:
        logger.warning("Web search handle resolution failed for %r: %s", name, exc)
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
                handle = _resolve_handle_via_web_search(row.name, city)
                if handle:
                    row.instagram_handle = handle
                    updated = True
                    logger.info("Resolved IG handle for %r: %s", row.name, handle)
            if updated:
                db.commit()


def discover_new_competitors(store_id: int) -> None:
    """Search for new competitors not already in the store's DB."""
    if not APIFY_TOKEN:
        logger.info("Competitor discovery skipped — APIFY_TOKEN not configured")
        return

    city, brand_name = _store_context(store_id)
    category = _store_category(store_id)

    from app.agents.scout.scrapers.web_scraper import search

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
            db.add(Competitor(store_id=store_id, name=name, source="discovered", city=city))
            existing_names.add(name.lower())
            added += 1
            logger.info("Discovered new competitor: %r (store=%d)", name, store_id)
        if added:
            db.commit()


def prune_stale_competitors(
    store_id: int,
    min_age_days: int = PRUNE_MIN_AGE_DAYS,
    min_findings: int = PRUNE_MIN_FINDINGS,
) -> int:
    """Remove auto-discovered competitors that have never produced real signal.

    Discovery only ever adds (see discover_new_competitors) -- nothing
    previously removed a bad auto-extracted name (e.g. a misparsed business
    name from a search snippet), so the discovered list only grows. Only
    source == "discovered" rows are touched; primary/seed entries are
    curated by the business and are never pruned regardless of performance.

    Two independent removal rules, both scoped to source == "discovered":
    1. Structurally junk names (_looks_like_junk_name -- truncated snippets,
       generic boilerplate, emoji) are removed immediately regardless of
       age or finding count. A mismatched Google Maps entity can still rack
       up real findings (confirmed live: "Cafe Near Me" pulled 289 reviews
       for what was almost certainly the wrong place) -- finding count
       alone doesn't prove a junk name is relevant, so this rule doesn't
       wait on it.
    2. Otherwise-well-formed names are pruned once they've existed for at
       least min_age_days (a fair chance across a few live runs, since
       freshness caching means a brand-new one may not have been scraped
       yet) and have fewer than min_findings Finding rows ever recorded
       against their name for this store.
    """
    cutoff = datetime.utcnow() - timedelta(days=min_age_days)
    pruned = 0
    with SessionLocal() as db:
        discovered = db.query(Competitor).filter(
            Competitor.store_id == store_id,
            Competitor.source == "discovered",
        ).all()
        for c in discovered:
            if _looks_like_junk_name(c.name):
                logger.info(
                    "Pruning junk-named competitor: %r (store=%d)", c.name, store_id,
                )
                db.delete(c)
                pruned += 1
                continue
            if c.created_at > cutoff:
                continue
            finding_count = db.query(Finding).filter(
                Finding.store_id == store_id,
                Finding.competitor_name == c.name,
            ).count()
            if finding_count < min_findings:
                logger.info(
                    "Pruning low-relevance competitor: %r (store=%d, findings=%d)",
                    c.name, store_id, finding_count,
                )
                db.delete(c)
                pruned += 1
        if pruned:
            db.commit()
    return pruned


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

# Search-result snippets get cut mid-parenthetical ("Cafe Sierra (Best...")
# or mid-punctuation far more often than a real business name ends this way.
_TRUNCATED_END = re.compile(r"[(\[,&:;/-]\s*$")

# Boilerplate that shows up in scraped snippets but is never itself a
# business name -- confirmed live: "Cafe Near Me" (289 reviews pulled for a
# mismatched Google Maps entity) and "TBC on Instagram" both passed every
# other check here.
_GENERIC_PHRASES = re.compile(
    r"\b(near me|on instagram|on facebook|on tiktok|to be confirmed|\btbc\b|"
    r"click here|read more|sponsored|coming soon)\b",
    re.I,
)

# Emoji range matching cleaning.py's _remove_emoji_noise -- a real business
# name basically never ships an emoji as part of the title text itself;
# in practice this catches a person's name a reviewer/local-guide left
# behind getting mis-scraped as if it were the business (e.g. "Jawahar
# Mustafa❤️", confirmed live).
_HAS_EMOJI = re.compile(
    r"[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
    r"\U0001F680-\U0001F6FF\U0001F700-\U0001F77F"
    r"\U0001F780-\U0001F7FF\U0001F800-\U0001F8FF"
    r"\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F"
    r"\U0001FA70-\U0001FAFF\U00002702-\U000027B0"
    r"\U000024C2-\U0001F251]"
)


def _looks_like_junk_name(candidate: str) -> bool:
    """Structural checks shared between discovery-time rejection (never add
    it) and pruning (remove it if it slipped through before this existed).
    Deliberately doesn't try to detect "looks like a human name" in
    general -- too easy to false-positive on a legitimately unusual
    restaurant name -- just the concrete junk patterns seen in practice."""
    if _TRUNCATED_END.search(candidate):
        return True
    if _GENERIC_PHRASES.search(candidate):
        return True
    if _HAS_EMOJI.search(candidate):
        return True
    return False


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
    if _looks_like_junk_name(candidate):
        return None
    words = candidate.split()
    if not any(w[0].isupper() for w in words if len(w) > 2):
        return None
    if len(words) > 5:
        return None
    return candidate


def get_all_competitors(store_id: int) -> list[dict]:
    """Return competitors for a store, filtered to the store's operating cities.

    Competitors with no city set are always included (primary/seed entries added
    before city tracking was introduced). City-tagged entries are included only
    if their city matches one of the store's StoreLocation cities.
    Primary competitors are sorted first.
    """
    store_cities = _store_cities(store_id)
    with SessionLocal() as db:
        rows = db.query(Competitor).filter(Competitor.store_id == store_id).all()

    def _include(r: Competitor) -> bool:
        if not r.city:
            return True  # untagged entries grandfathered in
        return r.city.strip().lower() in store_cities

    filtered = [r for r in rows if _include(r)]
    filtered.sort(key=lambda r: (0 if r.source == "primary" else 1, r.id))
    return [
        {
            "name": r.name,
            "category": r.category,
            "city": r.city,
            "instagram_handle": r.instagram_handle,
            "website": r.website,
            "place_id": r.place_id,
            "source": r.source,
        }
        for r in filtered
    ]


def seed_competitors_for_store(
    store_id: int,
    competitors: list[dict] | None = None,
) -> int:
    """Seed competitors into the DB for a store. Skips duplicates.

    If `competitors` is provided, uses that list. Otherwise falls back to the
    module-level COMPETITORS list (Sugar Rush defaults).
    """
    source_list = competitors if competitors is not None else COMPETITORS
    added = 0
    with SessionLocal() as db:
        existing = {
            c.name.lower()
            for c in db.query(Competitor).filter(Competitor.store_id == store_id).all()
        }
        for comp in source_list:
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
