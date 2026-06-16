"""POS-derived customer segments for campaign targeting.

Reuses the existing retention analysis to size an addressable audience and
attach an assumed redemption rate per segment. Recommendations only — there is
no contact list / opt-in / geo in the POS data, so actually *sending* would need
a CRM later (the campaign log is structured to plug into one).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.analysis.retention import analyze_retention
from app.models.canonical import MenuItem, Order, Staff
from app.revenue.config import (
    MIN_VISITS_FOR_PREMIUM,
    PREMIUM_SPEND_PERCENTILE,
    SEGMENT_LABELS,
    RevenueConfig,
)


@dataclass
class Segment:
    key: str
    label: str
    size: int
    avg_ticket: float
    redemption_rate: float

    def expected_redemptions(self, audience: int | None = None) -> int:
        n = self.size if audience is None else min(audience, self.size)
        return int(round(n * self.redemption_rate))


def build_segments(
    orders: list[Order],
    menu: dict[str, MenuItem],
    staff: dict[str, Staff],
    config: RevenueConfig | None = None,
) -> dict[str, Segment]:
    config = config or RevenueConfig()
    rep = analyze_retention(orders, menu, staff)
    customers = rep.customers
    if not customers:
        return {}

    repeat = [c for c in customers if c.visit_count >= MIN_VISITS_FOR_PREMIUM]
    spend_cut = _percentile(
        [c.total_spend for c in repeat], PREMIUM_SPEND_PERCENTILE
    ) if repeat else 0.0

    premium = [c for c in repeat if c.total_spend >= spend_cut]
    regulars = [
        c for c in customers
        if c.visit_count >= MIN_VISITS_FOR_PREMIUM
        and c.median_cadence_days is not None
        and c.median_cadence_days <= rep.cadence_threshold_days
    ]
    lapsing = [c for c in customers if c.is_lapsing]
    occasional = [c for c in customers if 1 <= c.visit_count <= 2]

    def seg(key: str, members) -> Segment:
        size = len(members)
        avg = sum(c.avg_ticket for c in members) / size if size else 0.0
        return Segment(
            key=key, label=SEGMENT_LABELS.get(key, key), size=size,
            avg_ticket=round(avg, 0),
            redemption_rate=config.segment_redemption.get(key, 0.03),
        )

    return {
        "premium": seg("premium", premium),
        "regular": seg("regular", regulars),
        "lapsing": seg("lapsing", lapsing),
        "occasional": seg("occasional", occasional),
    }


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(round((len(s) - 1) * q))
    return s[idx]
