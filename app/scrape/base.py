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
    """Anything that can turn targets into point-in-time snapshots.

    Implementations must be safe to call offline-or-online behind the factory:
    a failure to reach the network should raise, not return partial garbage,
    so the factory can fall back cleanly.
    """

    def scrape(
        self,
        targets: list[ScrapeTarget],
        scope: Scope,
    ) -> list[CompetitorSnapshot]:
        ...
