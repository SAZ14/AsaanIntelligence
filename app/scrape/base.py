from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.models.competitive import CompetitorSnapshot, Scope


@dataclass
class ScrapeTarget:
    """One thing to go and look at.

    A target names a rival (or a discovery query) and the platforms to read.
    For LOCAL scope these are concrete competitor pages; for NATIONAL /
    INTERNATIONAL scope `query` may be a trend-discovery search instead.
    """

    name: str
    area: str = ""
    city: str = "Islamabad"
    country: str = "Pakistan"
    source_urls: list[str] = field(default_factory=list)
    query: str = ""


@runtime_checkable
class Scraper(Protocol):
    """FROZEN CONTRACT — the only interface the core depends on for ingestion.

    A `Scraper` turns `ScrapeTarget`s into point-in-time `CompetitorSnapshot`s.
    Every implementation — `FixtureScraper` (offline) and `BrowserbaseScraper`
    (live) — MUST return the *identical* shape so that
    `app/analysis/competitive.py` never needs to know which one produced the
    data. Swapping one for the other is a drop-in with zero core changes.

    ── Return shape (exact) ──────────────────────────────────────────────────
    `scrape(targets, scope)` returns `list[CompetitorSnapshot]` where every
    element is a fully-validated `app.models.competitive.CompetitorSnapshot`.
    A snapshot is one rival captured at one moment, with these guarantees the
    analysis layer relies on:

      • `competitor: Competitor`
            - `competitor_id: str` — non-empty, STABLE across captures of the
              same rival (this is the join key for diffing and for the store).
            - `name, area, city, country: str` — present (may be "" except name).
            - `opened_at: datetime | None` — drives the `is_new` computed flag.
      • `captured_at: datetime` — when this capture was taken (naive or aware,
            but be consistent within a deployment; the store compares these).
      • `menu: list[CompetitorMenuItem]`
            - `name: str`, `category: str`, `price: float (>= 0)`,
              `tags: list[str]`. Each item exposes `norm_name` (computed).
      • `promotions: list[CompetitorPromotion]` — each exposes `is_active`.
      • `reviews: list[ReviewStanding]`
            - `source: str`, `rating_avg: float`, `review_count: int (>= 0)`.
            - Drives the `weighted_rating` / `total_reviews` computed fields.

    ── Invariants ────────────────────────────────────────────────────────────
      • `competitor_id` is UNIQUE within a single `scrape()` result.
      • The result is a (possibly empty) list — never `None`.
      • Prices, ratings, and counts are numeric and non-negative.

    ── Failure semantics ─────────────────────────────────────────────────────
    A failure to reach the network (or missing credentials / SDK) MUST raise,
    not return partial or empty garbage, so `get_scraper()` can fall back to
    the fixture scraper cleanly.

    Run `app.scrape.contract.assert_scraper_conforms(...)` against any new
    implementation to verify all of the above.
    """

    def scrape(
        self,
        targets: list[ScrapeTarget],
        scope: Scope,
    ) -> list[CompetitorSnapshot]:
        ...
