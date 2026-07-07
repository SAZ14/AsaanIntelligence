"""Tunable knobs for the Revenue agent.

Everything that is a business assumption — the brand-safe campaign catalogue,
segment redemption rates, pricing thresholds — lives here so it can be changed
without touching logic. All values can be overridden from a JSON file via
``RevenueConfig.load``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_VENUE_NAME = "the venue"

# Categories whose demand is habitual / routine → treated as more inelastic, so
# they carry pricing power. Everything else is discretionary.
HABITUAL_CATEGORIES = {"Coffee", "Tea"}

# Beverage categories (the "anchor" of a café order) vs add-on food, and
# categories that don't count as a real food upsell (soda / water).
BEVERAGE_CATEGORIES = {"Coffee", "Tea"}
ADDON_EXCLUDE_CATEGORIES = {"Other"}

# High-margin categories worth adding if the menu lacks them.
RECOMMENDED_CATEGORIES = ["Fresh juices", "Smoothies", "Iced teas"]

# Average-ticket levers.
TARGET_ATTACH_UPLIFT = 0.10          # realistic +10pp food-attach goal
ATTACH_CAPTURE_FACTOR = 0.6          # conservatively bank 60% of the modelled gain

# Loyalty / frequency levers.
LOYALTY_PUNCH_TARGET = 9             # buy 9, get the 10th
EXTRA_VISITS_PER_REGULAR = 0.5       # modelled monthly extra visits a card drives

# Pricing thresholds.
MIN_VOLUME_FOR_PRICING = 15          # ignore long-tail items for price advice
MIN_POWER_SCORE = 0.60               # only recommend raises above this score
MIN_RAISE_PCT = 0.03
MAX_RAISE_PCT = 0.10
VOLUME_RETENTION_ON_RAISE = 0.97     # conservative: assume small dip even when inelastic

# Customer segments and their assumed campaign redemption rates.
# (Brand-safe invitations convert better than blast discounts but we stay modest.)
SEGMENT_REDEMPTION = {
    "premium": 0.07,
    "regular": 0.05,
    "lapsing": 0.05,     # win-back of high-value lapsed regulars
    "occasional": 0.02,
}

SEGMENT_LABELS = {
    "premium": "premium regulars (top spenders)",
    "regular": "regulars",
    "lapsing": "lapsing regulars (win-back)",
    "occasional": "occasional guests",
}

# Premium = repeat customers in the top spend quartile.
PREMIUM_SPEND_PERCENTILE = 0.75
MIN_VISITS_FOR_PREMIUM = 3


@dataclass
class Campaign:
    key: str
    name: str                 # brand-safe phrasing shown to guests
    description: str          # what the owner is offering
    target_segment: str       # which segment it suits best
    est_cost_per_redemption: float   # PKR cost of honouring one redemption
    fits_dayparts: tuple[str, ...]   # daypart names this offer suits


# Brand-safe catalogue — never "20% off". (Names come straight from the brief.)
DEFAULT_CAMPAIGNS: list[Campaign] = [
    Campaign("coffee_tasting", "Invite-only coffee tasting for 2",
             "Curated tasting flight, hosted by the head barista",
             "premium", est_cost_per_redemption=350.0,
             fits_dayparts=("Afternoon", "Midday")),
    Campaign("chefs_dessert", "Chef's complimentary dessert",
             "A complimentary signature dessert with any visit",
             "regular", est_cost_per_redemption=180.0,
             fits_dayparts=("Afternoon", "Evening", "Midday")),
    Campaign("priority_table", "Priority table",
             "A held, best-in-house table, no wait",
             "premium", est_cost_per_redemption=0.0,
             fits_dayparts=("Evening", "Late night")),
    Campaign("members_mocktail", "Members-only mocktail",
             "A complimentary craft mocktail for members",
             "regular", est_cost_per_redemption=220.0,
             fits_dayparts=("Evening", "Late night", "Afternoon")),
    Campaign("founders_circle", "Founders' circle table",
             "An exclusive invitation to the Founders' circle evening",
             "premium", est_cost_per_redemption=500.0,
             fits_dayparts=("Evening", "Late night")),
]


@dataclass
class RevenueConfig:
    venue_name: str = DEFAULT_VENUE_NAME
    habitual_categories: set[str] = field(default_factory=lambda: set(HABITUAL_CATEGORIES))
    beverage_categories: set[str] = field(default_factory=lambda: set(BEVERAGE_CATEGORIES))
    addon_exclude_categories: set[str] = field(
        default_factory=lambda: set(ADDON_EXCLUDE_CATEGORIES)
    )
    recommended_categories: list[str] = field(
        default_factory=lambda: list(RECOMMENDED_CATEGORIES)
    )
    target_attach_uplift: float = TARGET_ATTACH_UPLIFT
    attach_capture_factor: float = ATTACH_CAPTURE_FACTOR
    loyalty_punch_target: int = LOYALTY_PUNCH_TARGET
    extra_visits_per_regular: float = EXTRA_VISITS_PER_REGULAR
    min_volume_for_pricing: int = MIN_VOLUME_FOR_PRICING
    min_power_score: float = MIN_POWER_SCORE
    min_raise_pct: float = MIN_RAISE_PCT
    max_raise_pct: float = MAX_RAISE_PCT
    volume_retention_on_raise: float = VOLUME_RETENTION_ON_RAISE
    segment_redemption: dict[str, float] = field(
        default_factory=lambda: dict(SEGMENT_REDEMPTION)
    )
    campaigns: list[Campaign] = field(default_factory=lambda: list(DEFAULT_CAMPAIGNS))

    def campaigns_for_daypart(self, daypart: str) -> list[Campaign]:
        fits = [c for c in self.campaigns if daypart in c.fits_dayparts]
        return fits or list(self.campaigns)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "RevenueConfig":
        cfg = cls()
        if path is None or not Path(path).exists():
            return cfg
        raw = json.loads(Path(path).read_text())
        if "venue_name" in raw:
            cfg.venue_name = raw["venue_name"]
        if "habitual_categories" in raw:
            cfg.habitual_categories = set(raw["habitual_categories"])
        for key in ("min_volume_for_pricing", "min_power_score", "min_raise_pct",
                    "max_raise_pct", "volume_retention_on_raise"):
            if key in raw:
                setattr(cfg, key, type(getattr(cfg, key))(raw[key]))
        if "segment_redemption" in raw:
            cfg.segment_redemption.update(raw["segment_redemption"])
        if "campaigns" in raw:
            cfg.campaigns = [
                Campaign(
                    key=c["key"], name=c["name"], description=c.get("description", ""),
                    target_segment=c.get("target_segment", "premium"),
                    est_cost_per_redemption=float(c.get("est_cost_per_redemption", 0.0)),
                    fits_dayparts=tuple(c.get("fits_dayparts", [])),
                )
                for c in raw["campaigns"]
            ]
        return cfg

