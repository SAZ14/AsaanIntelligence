"""Deterministic competitive analysis.

Everything here is pure data crunching — no LLM, no network — so it can be
unit-tested with fixed inputs and reasoned about exactly. The agent layer
(`app.agents.competitive_intel`) wraps these findings in owner-facing language.

The four LOCAL signals:
  • pricing      — where our prices sit vs the area average, per category.
  • new dishes   — items a rival is now selling that they weren't last capture.
  • promotions   — what discounts rivals are running right now.
  • review trends — who is gaining (or losing) review momentum.
Plus menu gaps (categories rivals cover that we don't) and, for wider scope,
national trend aggregation.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from statistics import mean

from app.models.canonical import MenuItem
from app.models.competitive import CompetitorSnapshot

# A category needs at least this many rival data points before we trust its
# "area average" enough to make a pricing recommendation.
MIN_PRICING_SAMPLE = 3

# How far from the area average counts as actionable rather than noise.
PRICING_BAND = 0.08  # 8%

# Review-count growth (vs previous capture) that reads as real momentum.
MOMENTUM_GROWTH_PCT = 0.15


@dataclass
class PricingInsight:
    category: str
    home_avg_price: float
    area_avg_price: float
    pct_diff: float            # (home - area) / area; negative = we're cheaper
    rival_sample: int
    recommendation: str        # "raise" | "hold" | "premium_risk"
    headline: str = ""


@dataclass
class NewDishAlert:
    competitor_id: str
    competitor_name: str
    area: str
    dish_name: str
    category: str
    price: float
    tags: list[str] = field(default_factory=list)
    is_new_venue: bool = False
    momentum: str = "steady"   # carried from the venue's review trend


@dataclass
class PromotionAlert:
    competitor_name: str
    area: str
    title: str
    description: str
    discount_pct: float | None = None


@dataclass
class ReviewTrendAlert:
    competitor_id: str
    competitor_name: str
    area: str
    rating_now: float
    rating_delta: float
    reviews_now: int
    reviews_delta: int
    growth_pct: float
    direction: str             # "rising" | "falling" | "steady"


@dataclass
class MenuGap:
    category: str
    rival_count: int           # how many rivals sell this category
    example_dishes: list[str] = field(default_factory=list)


@dataclass
class TrendSignal:
    label: str                 # a tag or category
    kind: str                  # "tag" | "category"
    venue_count: int
    example_dishes: list[str] = field(default_factory=list)


@dataclass
class CompetitiveReport:
    scope: str
    home_venue: str
    competitor_count: int
    pricing: list[PricingInsight] = field(default_factory=list)
    new_dishes: list[NewDishAlert] = field(default_factory=list)
    promotions: list[PromotionAlert] = field(default_factory=list)
    review_trends: list[ReviewTrendAlert] = field(default_factory=list)
    menu_gaps: list[MenuGap] = field(default_factory=list)
    national_trends: list[TrendSignal] = field(default_factory=list)
    headlines: list[str] = field(default_factory=list)
    advisory: list[str] = field(default_factory=list)


# ── Pricing vs area average ──

def _norm_category(c: str) -> str:
    return c.strip().lower()


def area_pricing(
    home_menu: dict[str, MenuItem],
    snapshots: list[CompetitorSnapshot],
) -> list[PricingInsight]:
    """Compare our per-category average price to the area average."""
    rival_prices: dict[str, list[float]] = defaultdict(list)
    for s in snapshots:
        for item in s.menu:
            if item.price > 0 and item.category:
                rival_prices[_norm_category(item.category)].append(item.price)

    home_prices: dict[str, list[float]] = defaultdict(list)
    for mi in home_menu.values():
        if mi.price > 0 and mi.category:
            home_prices[_norm_category(mi.category)].append(mi.price)

    insights: list[PricingInsight] = []
    for cat, prices in home_prices.items():
        rivals = rival_prices.get(cat, [])
        if len(rivals) < MIN_PRICING_SAMPLE:
            continue
        home_avg = mean(prices)
        area_avg = mean(rivals)
        if area_avg <= 0:
            continue
        pct = (home_avg - area_avg) / area_avg

        if pct < -PRICING_BAND:
            rec = "raise"
        elif pct > PRICING_BAND:
            rec = "premium_risk"
        else:
            rec = "hold"

        insights.append(PricingInsight(
            category=cat,
            home_avg_price=round(home_avg, 2),
            area_avg_price=round(area_avg, 2),
            pct_diff=round(pct, 4),
            rival_sample=len(rivals),
            recommendation=rec,
            headline=_pricing_headline(cat, pct, rec),
        ))

    # Biggest absolute gap first — that's where the money is.
    insights.sort(key=lambda i: abs(i.pct_diff), reverse=True)
    return insights


def _pricing_headline(cat: str, pct: float, rec: str) -> str:
    if rec == "raise":
        return (f"Your {cat} prices sit {abs(pct):.0%} below the area average — "
                f"there's room to charge more.")
    if rec == "premium_risk":
        return (f"Your {cat} prices run {pct:.0%} above the area average — "
                f"make sure the experience justifies the premium.")
    return f"Your {cat} prices are in line with the area (within {PRICING_BAND:.0%})."


# ── New-dish detection (diff vs previous capture) ──

def detect_new_dishes(
    current: list[CompetitorSnapshot],
    previous: dict[str, CompetitorSnapshot],
    momentum_by_competitor: dict[str, str] | None = None,
) -> list[NewDishAlert]:
    """Dishes present now that weren't in a rival's previous snapshot.

    A rival with no previous snapshot is treated as newly discovered: every
    dish counts as new only if the venue itself is flagged new, otherwise we
    stay silent (we can't distinguish 'new dish' from 'first time we looked').
    """
    momentum_by_competitor = momentum_by_competitor or {}
    alerts: list[NewDishAlert] = []
    for s in current:
        cid = s.competitor.competitor_id
        prev = previous.get(cid)
        if prev is None:
            if not s.competitor.is_new:
                continue
            prev_names: set[str] = set()
        else:
            prev_names = {m.norm_name for m in prev.menu}

        for item in s.menu:
            if item.norm_name in prev_names:
                continue
            alerts.append(NewDishAlert(
                competitor_id=cid,
                competitor_name=s.competitor.name,
                area=s.competitor.area,
                dish_name=item.name,
                category=item.category,
                price=item.price,
                tags=list(item.tags),
                is_new_venue=s.competitor.is_new,
                momentum=momentum_by_competitor.get(cid, "steady"),
            ))
    return alerts


# ── Active promotions ──

def active_promotions(current: list[CompetitorSnapshot]) -> list[PromotionAlert]:
    alerts: list[PromotionAlert] = []
    for s in current:
        for promo in s.promotions:
            if promo.is_active:
                alerts.append(PromotionAlert(
                    competitor_name=s.competitor.name,
                    area=s.competitor.area,
                    title=promo.title,
                    description=promo.description,
                    discount_pct=promo.discount_pct,
                ))
    alerts.sort(key=lambda a: a.discount_pct or 0, reverse=True)
    return alerts


# ── Review-momentum trends ──

def review_trends(
    current: list[CompetitorSnapshot],
    previous: dict[str, CompetitorSnapshot],
) -> list[ReviewTrendAlert]:
    alerts: list[ReviewTrendAlert] = []
    for s in current:
        cid = s.competitor.competitor_id
        prev = previous.get(cid)
        rating_now = s.weighted_rating
        reviews_now = s.total_reviews

        if prev is None:
            rating_delta = 0.0
            reviews_delta = 0
            growth = 0.0
            direction = "steady"
        else:
            rating_delta = rating_now - prev.weighted_rating
            reviews_delta = reviews_now - prev.total_reviews
            base = prev.total_reviews
            growth = (reviews_delta / base) if base > 0 else 0.0
            if growth >= MOMENTUM_GROWTH_PCT and rating_delta >= -0.1:
                direction = "rising"
            elif rating_delta <= -0.2 or growth < 0:
                direction = "falling"
            else:
                direction = "steady"

        alerts.append(ReviewTrendAlert(
            competitor_id=cid,
            competitor_name=s.competitor.name,
            area=s.competitor.area,
            rating_now=round(rating_now, 2),
            rating_delta=round(rating_delta, 2),
            reviews_now=reviews_now,
            reviews_delta=reviews_delta,
            growth_pct=round(growth, 4),
            direction=direction,
        ))
    alerts.sort(key=lambda a: a.growth_pct, reverse=True)
    return alerts


def momentum_map(trends: list[ReviewTrendAlert]) -> dict[str, str]:
    """Convenience: competitor_id -> direction, for tagging new-dish alerts."""
    return {t.competitor_id: t.direction for t in trends}


# ── Menu gaps ──

def menu_gaps(
    home_menu: dict[str, MenuItem],
    current: list[CompetitorSnapshot],
    min_rivals: int = 2,
) -> list[MenuGap]:
    """Categories several rivals sell that we don't carry at all."""
    home_cats = {_norm_category(mi.category) for mi in home_menu.values() if mi.category}

    rival_cat_venues: dict[str, set[str]] = defaultdict(set)
    rival_cat_examples: dict[str, list[str]] = defaultdict(list)
    for s in current:
        for item in s.menu:
            cat = _norm_category(item.category)
            if not cat:
                continue
            rival_cat_venues[cat].add(s.competitor.competitor_id)
            if item.name not in rival_cat_examples[cat]:
                rival_cat_examples[cat].append(item.name)

    gaps: list[MenuGap] = []
    for cat, venues in rival_cat_venues.items():
        if cat in home_cats:
            continue
        if len(venues) < min_rivals:
            continue
        gaps.append(MenuGap(
            category=cat,
            rival_count=len(venues),
            example_dishes=rival_cat_examples[cat][:3],
        ))
    gaps.sort(key=lambda g: g.rival_count, reverse=True)
    return gaps


# ── National trend aggregation (NATIONAL scope) ──

def national_trends(
    current: list[CompetitorSnapshot],
    min_venues: int = 2,
) -> list[TrendSignal]:
    """Tags / categories appearing across many venues = a movement worth noting."""
    tag_venues: dict[str, set[str]] = defaultdict(set)
    tag_examples: dict[str, list[str]] = defaultdict(list)
    cat_venues: dict[str, set[str]] = defaultdict(set)
    cat_examples: dict[str, list[str]] = defaultdict(list)

    for s in current:
        cid = s.competitor.competitor_id
        for item in s.menu:
            for tag in item.tags:
                t = tag.strip().lower()
                tag_venues[t].add(cid)
                if item.name not in tag_examples[t]:
                    tag_examples[t].append(item.name)
            cat = _norm_category(item.category)
            if cat:
                cat_venues[cat].add(cid)
                if item.name not in cat_examples[cat]:
                    cat_examples[cat].append(item.name)

    signals: list[TrendSignal] = []
    for tag, venues in tag_venues.items():
        if len(venues) >= min_venues:
            signals.append(TrendSignal(
                label=tag, kind="tag", venue_count=len(venues),
                example_dishes=tag_examples[tag][:3],
            ))
    for cat, venues in cat_venues.items():
        if len(venues) >= min_venues:
            signals.append(TrendSignal(
                label=cat, kind="category", venue_count=len(venues),
                example_dishes=cat_examples[cat][:3],
            ))

    signals.sort(key=lambda s: s.venue_count, reverse=True)
    return signals
