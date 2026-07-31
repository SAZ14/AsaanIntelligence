from __future__ import annotations

import json
from pathlib import Path

from app.models.competitive import CompetitorSnapshot, Scope
from app.scrape.base import ScrapeTarget

DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "competitive"


class FixtureScraper:
    """Offline scraper that replays recorded snapshots from JSON fixtures.

    Used automatically when no Browserbase credentials are configured, and in
    tests. It reads a snapshot file shaped like the live scraper's output, so
    downstream analysis cannot tell the difference.
    """

    def __init__(self, snapshot_file: Path | str | None = None) -> None:
        if snapshot_file is None:
            snapshot_file = DEFAULT_FIXTURE_DIR / "snapshot_current.json"
        self.snapshot_file = Path(snapshot_file)

    def scrape(
        self,
        targets: list[ScrapeTarget],
        scope: Scope,
    ) -> list[CompetitorSnapshot]:
        snaps = load_snapshots_file(self.snapshot_file)

        # Honour scope by filtering the recorded universe the same way a live
        # crawl would only visit in-scope venues.
        if scope == Scope.LOCAL and targets:
            areas = {t.area for t in targets if t.area}
            cities = {t.city for t in targets if t.city}
            if areas:
                snaps = [s for s in snaps if s.competitor.area in areas
                         or s.competitor.city in cities]
        elif scope == Scope.NATIONAL:
            countries = {t.country for t in targets if t.country} or {"Pakistan"}
            snaps = [s for s in snaps if s.competitor.country in countries]
        return snaps


def load_snapshots_file(path: Path | str) -> list[CompetitorSnapshot]:
    """Parse a snapshot fixture file into validated models."""
    path = Path(path)
    raw = json.loads(path.read_text())
    return [CompetitorSnapshot.model_validate(item) for item in raw["competitors"]]
