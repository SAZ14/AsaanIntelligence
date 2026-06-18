"""Executable conformance checks for the `Scraper` contract.

This is the contract in `app/scrape/base.py` made runnable. Point it at ANY
scraper — `FixtureScraper` today, `BrowserbaseScraper` once credentials exist —
and it verifies the return shape and invariants the core relies on. If a new
scraper passes `assert_scraper_conforms`, it is a guaranteed drop-in for
`app/analysis/competitive.py`.
"""

from __future__ import annotations

from datetime import datetime

from app.models.competitive import (
    CompetitorMenuItem,
    CompetitorPromotion,
    CompetitorSnapshot,
    ReviewStanding,
    Scope,
)
from app.scrape.base import Scraper, ScrapeTarget


def snapshot_violations(snap: object) -> list[str]:
    """Return a list of contract violations for one snapshot ([] == conformant)."""
    v: list[str] = []

    if not isinstance(snap, CompetitorSnapshot):
        return [f"not a CompetitorSnapshot: {type(snap).__name__}"]

    if not isinstance(snap.captured_at, datetime):
        v.append(f"captured_at must be datetime, got {type(snap.captured_at).__name__}")

    c = snap.competitor
    if not isinstance(c.competitor_id, str) or not c.competitor_id:
        v.append("competitor.competitor_id must be a non-empty str")
    if not isinstance(c.name, str) or not c.name:
        v.append("competitor.name must be a non-empty str")
    for fld in ("area", "city", "country"):
        if not isinstance(getattr(c, fld), str):
            v.append(f"competitor.{fld} must be a str")
    if c.opened_at is not None and not isinstance(c.opened_at, datetime):
        v.append("competitor.opened_at must be datetime or None")
    # Computed flag must be reachable and boolean.
    if not isinstance(c.is_new, bool):
        v.append("competitor.is_new must be computable as bool")

    for i, item in enumerate(snap.menu):
        if not isinstance(item, CompetitorMenuItem):
            v.append(f"menu[{i}] is not a CompetitorMenuItem")
            continue
        if not isinstance(item.name, str) or not item.name:
            v.append(f"menu[{i}].name must be a non-empty str")
        if not isinstance(item.category, str):
            v.append(f"menu[{i}].category must be a str")
        if not isinstance(item.price, (int, float)) or item.price < 0:
            v.append(f"menu[{i}].price must be a number >= 0")
        if not isinstance(item.tags, list) or not all(isinstance(t, str) for t in item.tags):
            v.append(f"menu[{i}].tags must be list[str]")
        if not item.norm_name:
            v.append(f"menu[{i}].norm_name must be non-empty")

    for i, promo in enumerate(snap.promotions):
        if not isinstance(promo, CompetitorPromotion):
            v.append(f"promotions[{i}] is not a CompetitorPromotion")
            continue
        if not isinstance(promo.is_active, bool):
            v.append(f"promotions[{i}].is_active must be computable as bool")

    for i, r in enumerate(snap.reviews):
        if not isinstance(r, ReviewStanding):
            v.append(f"reviews[{i}] is not a ReviewStanding")
            continue
        if not isinstance(r.source, str) or not r.source:
            v.append(f"reviews[{i}].source must be a non-empty str")
        if not isinstance(r.rating_avg, (int, float)) or not (0 <= r.rating_avg <= 5):
            v.append(f"reviews[{i}].rating_avg must be a number in [0, 5]")
        if not isinstance(r.review_count, int) or r.review_count < 0:
            v.append(f"reviews[{i}].review_count must be an int >= 0")

    # Computed aggregates must be reachable.
    try:
        _ = snap.total_reviews
        _ = snap.weighted_rating
    except Exception as e:  # pragma: no cover - defensive
        v.append(f"computed review aggregates raised: {e}")

    return v


def assert_scraper_conforms(
    scraper: Scraper,
    targets: list[ScrapeTarget],
    scope: Scope,
) -> list[CompetitorSnapshot]:
    """Assert `scraper` honours the frozen contract; return its snapshots.

    Raises AssertionError with a precise reason on any violation. Safe to call
    against a live scraper in an integration test once credentials are present.
    """
    assert isinstance(scraper, Scraper), (
        f"{type(scraper).__name__} does not satisfy the Scraper protocol "
        "(missing scrape method)"
    )

    result = scraper.scrape(targets, scope)
    assert isinstance(result, list), f"scrape() must return a list, got {type(result).__name__}"

    seen_ids: set[str] = set()
    for idx, snap in enumerate(result):
        problems = snapshot_violations(snap)
        assert not problems, f"snapshot[{idx}] violates contract:\n  - " + "\n  - ".join(problems)
        cid = snap.competitor.competitor_id
        assert cid not in seen_ids, f"duplicate competitor_id within one scrape: {cid!r}"
        seen_ids.add(cid)

    return result
