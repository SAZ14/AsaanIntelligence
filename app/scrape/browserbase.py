from __future__ import annotations

import json
import os
from datetime import datetime

from app.models.competitive import (
    Competitor,
    CompetitorMenuItem,
    CompetitorPromotion,
    CompetitorSnapshot,
    ReviewStanding,
    Scope,
)
from app.scrape.base import ScrapeTarget


class BrowserbaseUnavailable(RuntimeError):
    """Raised when Browserbase cannot be used (missing creds / SDK / network)."""


class BrowserbaseScraper:
    """Live scraper backed by a Browserbase headless-browser session.

    Browserbase drives a real, cloud-hosted Chromium so we can render and read
    JS-heavy delivery/review pages (Foodpanda, Google, the venue's own site).
    The SDK and credentials are resolved lazily so importing this module never
    fails offline — only *using* it does, and the factory catches that.

    The extraction step asks Claude to turn page text into structured snapshot
    JSON, which keeps the scraper resilient to per-site markup churn.
    """

    def __init__(
        self,
        api_key: str | None = None,
        project_id: str | None = None,
        anthropic_client=None,
        extract_model: str = "claude-sonnet-4-6",
    ) -> None:
        self.api_key = api_key or os.environ.get("BROWSERBASE_API_KEY", "")
        self.project_id = project_id or os.environ.get("BROWSERBASE_PROJECT_ID", "")
        self.extract_model = extract_model
        self._anthropic = anthropic_client
        if not self.api_key or not self.project_id:
            raise BrowserbaseUnavailable(
                "BROWSERBASE_API_KEY / BROWSERBASE_PROJECT_ID not set"
            )

    # ── public API ──

    def scrape(
        self,
        targets: list[ScrapeTarget],
        scope: Scope,
    ) -> list[CompetitorSnapshot]:
        snapshots: list[CompetitorSnapshot] = []
        for target in targets:
            try:
                page_texts = [self._fetch_page(url) for url in target.source_urls]
            except Exception as e:  # pragma: no cover - network path
                raise BrowserbaseUnavailable(f"page fetch failed: {e}") from e
            snapshots.append(self._extract_snapshot(target, page_texts))
        return snapshots

    # ── internals ──

    def _session(self):  # pragma: no cover - requires network + creds
        try:
            from browserbase import Browserbase
        except ImportError as e:
            raise BrowserbaseUnavailable(
                "browserbase SDK not installed (pip install browserbase)"
            ) from e
        return Browserbase(api_key=self.api_key)

    def _fetch_page(self, url: str) -> str:  # pragma: no cover - network path
        """Render `url` in a Browserbase session and return its visible text.

        Uses Playwright over the Browserbase CDP endpoint. Kept deliberately
        small; site-specific selectors live in the extraction prompt, not here.
        """
        from playwright.sync_api import sync_playwright

        bb = self._session()
        session = bb.sessions.create(project_id=self.project_id)
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(session.connect_url)
            try:
                page = browser.contexts[0].pages[0]
                page.goto(url, wait_until="networkidle", timeout=45_000)
                return page.inner_text("body")
            finally:
                browser.close()

    def _client(self):  # pragma: no cover - requires creds
        if self._anthropic is None:
            import anthropic

            self._anthropic = anthropic.Anthropic()
        return self._anthropic

    def _extract_snapshot(
        self,
        target: ScrapeTarget,
        page_texts: list[str],
    ) -> CompetitorSnapshot:  # pragma: no cover - requires creds
        joined = "\n\n---PAGE---\n\n".join(page_texts)[:60_000]
        prompt = (
            "Extract structured competitive data from these restaurant pages. "
            "Return ONLY JSON with keys: menu (list of {name, category, price, "
            "tags}), promotions (list of {title, description, discount_pct}), "
            "reviews (list of {source, rating_avg, review_count}), "
            "opened_at (ISO date or null).\n\n"
            f"Venue: {target.name} ({target.area}, {target.city})\n\n{joined}"
        )
        resp = self._client().messages.create(
            model=self.extract_model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        data = json.loads(resp.content[0].text)
        return _snapshot_from_extracted(target, data)


def _snapshot_from_extracted(target: ScrapeTarget, data: dict) -> CompetitorSnapshot:
    """Build a validated snapshot from the LLM's extracted JSON.

    Pure and free of any network/SDK use so it can be unit-tested directly.
    """
    opened = data.get("opened_at")
    competitor = Competitor(
        competitor_id=f"{target.city}:{target.name}".lower().replace(" ", "-"),
        name=target.name,
        area=target.area,
        city=target.city,
        country=target.country,
        source_url=target.source_urls[0] if target.source_urls else "",
        opened_at=datetime.fromisoformat(opened) if opened else None,
    )
    menu = [
        CompetitorMenuItem(
            name=m["name"],
            category=m.get("category", ""),
            price=float(m.get("price") or 0.0),
            tags=m.get("tags", []),
        )
        for m in data.get("menu", [])
    ]
    promos = [
        CompetitorPromotion(
            title=p["title"],
            description=p.get("description", ""),
            discount_pct=p.get("discount_pct"),
        )
        for p in data.get("promotions", [])
    ]
    reviews = [
        ReviewStanding(
            source=r["source"],
            rating_avg=float(r.get("rating_avg") or 0.0),
            review_count=int(r.get("review_count") or 0),
        )
        for r in data.get("reviews", [])
    ]
    return CompetitorSnapshot(
        competitor=competitor,
        captured_at=datetime.now(),
        menu=menu,
        promotions=promos,
        reviews=reviews,
    )
