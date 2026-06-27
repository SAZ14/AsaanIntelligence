from __future__ import annotations
import hashlib
import logging

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.agents.scout.config import APIFY_TOKEN, APIFY_WEBSITE_ACTOR, APIFY_SEARCH_ACTOR
from app.agents.scout.schemas import FindingSchema

logger = logging.getLogger(__name__)


def _make_hash(competitor_name: str, text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha1(f"{competitor_name}||{normalized}".encode()).hexdigest()


def _infer_update_type(text: str) -> str:
    lower = text.lower()
    if any(w in lower for w in ["off", "deal", "sale", "discount", "bogo", "free", "%"]):
        return "discount"
    if any(w in lower for w in ["new", "introducing", "launch", "now available", "just dropped"]):
        return "new_product"
    if any(w in lower for w in ["menu", "price", "cost", "pkr", "rs.", "rs "]):
        return "menu_change"
    return "post"


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=2, min=5, max=30),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _run_website_actor(url: str) -> list[dict]:
    from apify_client import ApifyClient
    client = ApifyClient(APIFY_TOKEN)
    run = client.actor(APIFY_WEBSITE_ACTOR).call(run_input={
        "startUrls": [{"url": url}],
        "crawlerType": "cheerio",
        "maxCrawlPages": 3,
        "maxCrawlDepth": 1,
        "outputFormats": ["markdown"],
    })
    return list(client.dataset(run["defaultDatasetId"]).iterate_items())


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=2, min=5, max=30),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _run_search_actor(query: str, num_results: int) -> list[dict]:
    from apify_client import ApifyClient
    client = ApifyClient(APIFY_TOKEN)
    run = client.actor(APIFY_SEARCH_ACTOR).call(run_input={
        "queries": query,
        "maxPagesPerQuery": 1,
        "resultsPerPage": num_results,
        "countryCode": "pk",
    })
    return list(client.dataset(run["defaultDatasetId"]).iterate_items())


def search(query: str, limit: int = 5) -> list[dict]:
    """Search the web; returns [{url, title, description}]. Used by discovery."""
    if not APIFY_TOKEN:
        return []
    try:
        items = _run_search_actor(query, num_results=limit)
        results: list[dict] = []
        for item in items:
            organic = item.get("organicResults", [])
            if organic:
                for r in organic[:limit]:
                    results.append({
                        "url": r.get("url", ""),
                        "title": r.get("title", ""),
                        "description": r.get("description", r.get("snippet", "")),
                    })
            else:
                results.append({
                    "url": item.get("url", ""),
                    "title": item.get("title", ""),
                    "description": item.get("description", item.get("snippet", "")),
                })
        return results[:limit]
    except Exception as exc:
        logger.warning("Web search failed for %r: %s", query, exc)
        return []


def find_menu_and_offers(competitor: dict) -> list[FindingSchema]:
    """Crawl competitor website and search for recent offers via Apify."""
    if not APIFY_TOKEN:
        logger.warning("Web scraper disabled — no APIFY_TOKEN")
        return []

    findings: list[FindingSchema] = []
    name = competitor["name"]
    website = competitor.get("website")

    if website:
        try:
            items = _run_website_actor(website)
            for item in items:
                md = item.get("markdown") or item.get("text") or ""
                url = item.get("url", website)
                if not md or len(md.strip()) < 50:
                    continue
                text = md[:1000]
                findings.append(FindingSchema(
                    competitor_name=name,
                    source_platform="website",
                    update_type=_infer_update_type(text),
                    content_text=text,
                    source_url=url,
                    content_hash=_make_hash(name, text),
                ))
        except Exception as exc:
            logger.warning("Website crawl failed for %s: %s", website, exc)

    for query in [
        f"{name} Islamabad new offer 2026",
        f"{name} Islamabad new product launch",
    ]:
        for r in search(query, limit=3):
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
