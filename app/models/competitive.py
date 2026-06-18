"""Canonical models for the Competitive Intelligence agent.

These mirror the style of `app.models.canonical` — Pydantic v2 models with
sensible defaults — but describe *rival* venues rather than our own POS data.

A `CompetitorSnapshot` is a point-in-time capture of one rival: its menu,
active promotions, and aggregate review standing. Storing snapshots over time
is what lets us detect *change* — new dishes, price moves, review momentum.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, computed_field


class Scope(str, Enum):
    """How wide a net the agent casts when looking at the competition.

    LOCAL          — direct rivals in the same city/sector (head-to-head).
    NATIONAL       — what is trending across the country, to adopt early.
    INTERNATIONAL  — advisory: global food trends worth importing.
    """

    LOCAL = "local"
    NATIONAL = "national"
    INTERNATIONAL = "international"


class Competitor(BaseModel):
    competitor_id: str
    name: str
    area: str = ""          # e.g. "F-7" (sector / neighbourhood)
    city: str = "Islamabad"
    country: str = "Pakistan"
    cuisine: str = ""
    source_url: str = ""
    opened_at: datetime | None = None   # known opening date, if scraped

    @computed_field
    @property
    def is_new(self) -> bool:
        """A venue opened within the last 90 days reads as 'the new spot'."""
        if self.opened_at is None:
            return False
        return (datetime.now() - self.opened_at).days <= 90


class CompetitorMenuItem(BaseModel):
    name: str
    category: str = ""
    price: float = 0.0
    currency: str = "PKR"
    tags: list[str] = Field(default_factory=list)   # e.g. ["dessert", "viral"]

    @computed_field
    @property
    def norm_name(self) -> str:
        """Normalised key used for diffing snapshots across time."""
        return " ".join(self.name.lower().split())


class CompetitorPromotion(BaseModel):
    title: str
    description: str = ""
    discount_pct: float | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @computed_field
    @property
    def is_active(self) -> bool:
        now = datetime.now()
        if self.starts_at and now < self.starts_at:
            return False
        if self.ends_at and now > self.ends_at:
            return False
        return True


class ReviewStanding(BaseModel):
    """Aggregate review position on one platform at snapshot time."""

    source: str                 # "Google", "Foodpanda", etc.
    rating_avg: float = 0.0
    review_count: int = 0


class CompetitorSnapshot(BaseModel):
    """One rival, captured at one moment."""

    competitor: Competitor
    captured_at: datetime
    menu: list[CompetitorMenuItem] = Field(default_factory=list)
    promotions: list[CompetitorPromotion] = Field(default_factory=list)
    reviews: list[ReviewStanding] = Field(default_factory=list)

    @computed_field
    @property
    def total_reviews(self) -> int:
        return sum(r.review_count for r in self.reviews)

    @computed_field
    @property
    def weighted_rating(self) -> float:
        total = self.total_reviews
        if total == 0:
            return 0.0
        return sum(r.rating_avg * r.review_count for r in self.reviews) / total
