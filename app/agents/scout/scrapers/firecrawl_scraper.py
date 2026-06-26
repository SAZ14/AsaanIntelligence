from __future__ import annotations
import hashlib
import logging
import os
from datetime import datetime
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.agents.scout.config import FIRECRAWL_API_KEY
from app.agents.scout.schemas import FindingSchema

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        if not FIRECRAWL_API_KEY:
            raise RuntimeError("FIRECRAWL_API_KEY not set")
        from firecrawl import V1FirecrawlApp
        _client = V1FirecrawlApp(api_key=FIRECRAWL_API_KEY)
    return _client


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _search_raw(query: str, limit: int = 5) -> list[dict]:
    client = _get_client()
    result = client.search(query, limit=limit)
    # V1SearchResponse: result.data is List[Dict] where each dict is a V1FirecrawlDocument
    if hasattr(result, "data") and isinstance(result.data, list):
        return [item if isinstance(item, dict) else item.model_dump() for item in result.data]
    if isinstance(result, list):
        return result
    return []


def search(query: str, limit: int = 5) -> list[dict]:
    try:
        items = _search_raw(query, limit=limit)
        out = []
        for item in items:
            if isinstance(item, dict):
                out.append({
                    "url": item.get("url", ""),
                    "title": item.get("title", ""),
                    "description": item.get("description", item.get("markdown", "")),
                })
        return out
    except Exception as exc:
        logger.warning("Firecrawl search failed for %r: %s", query, exc)
        return []


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _scrape_raw(url: str) -> Optional[str]:
    client = _get_client()
    # V1FirecrawlApp.scrape_url returns V1ScrapeResponse which has .markdown directly
    result = client.scrape_url(url, formats=["markdown"])
    if hasattr(result, "markdown"):
        return result.markdown
    if isinstance(result, dict):
        return result.get("markdown") or result.get("data", {}).get("markdown")
    return None


def scrape_site(url: str) -> Optional[str]:
    try:
        return _scrape_raw(url)
    except Exception as exc:
        logger.warning("Firecrawl scrape failed for %s: %s", url, exc)
        return None


def _infer_update_type(text: str) -> str:
    lower = text.lower()
    if any(w in lower for w in ["off", "deal", "sale", "discount", "bogo", "free", "%"]):
        return "discount"
    if any(w in lower for w in ["new", "introducing", "launch", "now available", "just dropped"]):
        return "new_product"
    if any(w in lower for w in ["menu", "price", "cost", "pkr", "rs.", "rs "]):
        return "menu_change"
    return "post"


def _make_hash(competitor_name: str, text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha1(f"{competitor_name}||{normalized}".encode()).hexdigest()


def find_menu_and_offers(competitor: dict) -> list[FindingSchema]:
    findings: list[FindingSchema] = []
    website = competitor.get("website")
    name = competitor["name"]

    if not FIRECRAWL_API_KEY:
        logger.warning("Firecrawl disabled — no API key")
        return []

    # Scrape the main website
    if website:
        md = scrape_site(website)
        if md and len(md.strip()) > 50:
            text = md[:1000]
            findings.append(FindingSchema(
                competitor_name=name,
                source_platform="website",
                update_type=_infer_update_type(text),
                content_text=text,
                source_url=website,
                content_hash=_make_hash(name, text),
            ))
            # Try to find a menu/offers sub-page from the scraped markdown
            _try_menu_page(name, website, md, findings)

    # Web search for recent news/offers
    queries = [
        f"{name} Islamabad new offer 2026",
        f"{name} Islamabad new product launch",
    ]
    for q in queries:
        results = search(q, limit=3)
        for r in results:
            desc = r.get("description", "")
            if len(desc.strip()) < 30:
                continue
            text = desc[:1000]
            findings.append(FindingSchema(
                competitor_name=name,
                source_platform="web_search",
                update_type=_infer_update_type(text),
                content_text=text,
                source_url=r.get("url"),
                content_hash=_make_hash(name, text),
            ))

    return findings


def _try_menu_page(name: str, base_url: str, homepage_md: str, findings: list[FindingSchema]) -> None:
    for keyword in ["/menu", "/offers", "/deals", "/products"]:
        if keyword in homepage_md.lower():
            candidate = base_url.rstrip("/") + keyword
            md = scrape_site(candidate)
            if md and len(md.strip()) > 80:
                text = md[:1000]
                findings.append(FindingSchema(
                    competitor_name=name,
                    source_platform="website",
                    update_type=_infer_update_type(text),
                    content_text=text,
                    source_url=candidate,
                    content_hash=_make_hash(name, text),
                ))
            break

