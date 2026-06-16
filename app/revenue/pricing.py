"""Transparent "pricing power" scoring.

The dataset has one fixed price per item (no price changes), so true elasticity
can't be measured. Instead we score how *defensible* a price increase is from
observable POS signals, and only recommend raises for items that clearly carry
pricing power. Every component is explainable to the owner.

Signals (each 0..1, weighted):
  * habitual category    routine purchases (coffee/tea) resist price changes
  * demand stability     steady daily volume ⇒ demand is not price-twitchy
  * low discount-reliance items that rarely need a discount can take a raise
  * headroom vs peers     priced at/below category peers ⇒ room to move up
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from app.models.canonical import MenuItem, Order
from app.revenue.config import RevenueConfig

# Weights for the four signals (sum to 1.0).
W_HABITUAL = 0.30
W_STABILITY = 0.25
W_LOW_DISCOUNT = 0.25
W_HEADROOM = 0.20


@dataclass
class PricingRec:
    sku: str
    name: str
    category: str
    units: int
    current_price: float
    suggested_price: float
    raise_pct: float
    power_score: float
    est_monthly_uplift: float
    reasons: list[str] = field(default_factory=list)


def compute_pricing_recommendations(
    orders: list[Order],
    menu: dict[str, MenuItem],
    config: RevenueConfig | None = None,
    period_days: int = 30,
) -> list[PricingRec]:
    config = config or RevenueConfig()

    # Per-SKU aggregates over the window.
    units: dict[str, int] = defaultdict(int)
    gross: dict[str, float] = defaultdict(float)         # pre-discount line value
    discount: dict[str, float] = defaultdict(float)
    daily_units: dict[str, dict[date, int]] = defaultdict(lambda: defaultdict(int))

    for o in orders:
        d = o.datetime.date()
        for li in o.line_items:
            if li.is_void or li.is_comp:
                continue
            units[li.item_sku] += li.qty
            gross[li.item_sku] += li.line_amount + li.discount_amount
            discount[li.item_sku] += li.discount_amount
            daily_units[li.item_sku][d] += li.qty

    # Category peer prices for the headroom signal.
    cat_prices: dict[str, list[float]] = defaultdict(list)
    for mi in menu.values():
        cat_prices[mi.category].append(mi.price)

    recs: list[PricingRec] = []
    span = max(period_days, 1)

    for sku, vol in units.items():
        mi = menu.get(sku)
        if mi is None or vol < config.min_volume_for_pricing:
            continue

        reasons: list[str] = []

        # 1) habitual category
        habitual = 1.0 if mi.category in config.habitual_categories else 0.4
        if habitual == 1.0:
            reasons.append(f"{mi.category} is a habitual purchase (resists price change)")

        # 2) demand stability — steadier daily volume ⇒ higher pricing power
        series = list(daily_units[sku].values())
        stability = _stability(series)
        if stability >= 0.6:
            reasons.append("steady daily demand")

        # 3) low discount reliance
        disc_share = discount[sku] / gross[sku] if gross[sku] else 0.0
        low_discount = _clamp(1.0 - disc_share * 5.0, 0.0, 1.0)  # 20% disc share ⇒ 0
        if disc_share < 0.02:
            reasons.append("rarely discounted")

        # 4) headroom vs category peers
        peers = sorted(cat_prices.get(mi.category, [mi.price]))
        headroom = _headroom(mi.price, peers)
        if headroom >= 0.6:
            reasons.append("priced at or below similar items")

        score = (W_HABITUAL * habitual + W_STABILITY * stability
                 + W_LOW_DISCOUNT * low_discount + W_HEADROOM * headroom)

        if score < config.min_power_score:
            continue

        raise_pct = round(
            _clamp(config.min_raise_pct + (config.max_raise_pct - config.min_raise_pct) * score,
                   config.min_raise_pct, config.max_raise_pct),
            3,
        )
        suggested = _round_price(mi.price * (1 + raise_pct))
        per_unit = suggested - mi.price
        monthly_units = vol * (30.0 / span)
        uplift = per_unit * monthly_units * config.volume_retention_on_raise

        recs.append(PricingRec(
            sku=sku, name=mi.name, category=mi.category, units=vol,
            current_price=mi.price, suggested_price=suggested, raise_pct=raise_pct,
            power_score=round(score, 3), est_monthly_uplift=round(uplift, 0),
            reasons=reasons,
        ))

    recs.sort(key=lambda r: r.est_monthly_uplift, reverse=True)
    return recs


@dataclass
class PriceMove:
    """A simple, concrete price change: 'sells a lot → nudge it up PKR X'."""
    name: str
    category: str
    units_month: int
    direction: str            # "up" | "down"
    bump: float               # rupees to add (up) or take off (down)
    current_price: float
    new_price: float
    monthly_impact: float     # added monthly revenue (0 for cautious "down" hints)
    reason: str


def simple_price_moves(
    orders: list[Order],
    menu: dict[str, MenuItem],
    config: RevenueConfig | None = None,
    period_days: int = 30,
    max_up: int = 5,
) -> list[PriceMove]:
    """Plain-English price moves straight from what sells.

    Increases: items with clear pricing power get a small, tidy rupee bump
    (PKR 10/20/50 by price band); since they sell regardless, volume is assumed
    to hold, so added monthly revenue ≈ bump × monthly units.
    Decrease: one cautious hint for a priciest-in-category slow mover.
    """
    config = config or RevenueConfig()
    span = max(period_days, 1)

    units: dict[str, int] = defaultdict(int)
    for o in orders:
        for li in o.line_items:
            if not li.is_void and not li.is_comp:
                units[li.item_sku] += li.qty

    moves: list[PriceMove] = []

    # Increases — reuse the pricing-power screen, present them simply.
    for r in compute_pricing_recommendations(orders, menu, config, period_days)[:max_up]:
        bump = _simple_bump(r.current_price)
        m_units = round(units[r.sku] * (30.0 / span))
        impact = round(bump * m_units * config.volume_retention_on_raise, 0)
        moves.append(PriceMove(
            name=r.name, category=r.category, units_month=m_units, direction="up",
            bump=bump, current_price=r.current_price, new_price=r.current_price + bump,
            monthly_impact=impact,
            reason=f"sells ~{m_units}/mo and demand is steady",
        ))

    down = _decrease_candidate(units, menu, span)
    if down:
        moves.append(down)
    return moves


def _simple_bump(price: float) -> float:
    if price < 700:
        return 10.0
    if price < 1200:
        return 20.0
    return 50.0


def _decrease_candidate(units: dict[str, int], menu, span: int) -> "PriceMove | None":
    sold = {sku: v for sku, v in units.items() if v > 0}
    if len(sold) < 4:
        return None
    vols = sorted(sold.values())
    low_cut = vols[len(vols) // 4]            # 25th percentile of volume
    best = None
    for sku, mi in menu.items():
        v = units.get(sku, 0)
        if v <= 0 or v > low_cut:
            continue
        peers = [m2.price for m2 in menu.values() if m2.category == mi.category]
        if len(peers) >= 3 and mi.price == max(peers):
            if best is None or v < best[0]:
                best = (v, mi)
    if not best:
        return None
    v, mi = best
    bump = _simple_bump(mi.price)
    m_units = round(v * 30.0 / span)
    return PriceMove(
        name=mi.name, category=mi.category, units_month=m_units, direction="down",
        bump=bump, current_price=mi.price, new_price=mi.price - bump,
        monthly_impact=0.0,
        reason=f"priciest {mi.category.lower()} item and slow (~{m_units}/mo) — "
               f"a small cut may move more",
    )


def _stability(series: list[int]) -> float:
    """1.0 = perfectly steady daily volume, →0 as it gets spiky."""
    if len(series) < 2:
        return 0.5
    mean = statistics.mean(series)
    if mean <= 0:
        return 0.0
    cv = statistics.pstdev(series) / mean
    return _clamp(1.0 - cv, 0.0, 1.0)


def _headroom(price: float, peers: list[float]) -> float:
    """Room to raise relative to category peers: 1.0 at the cheapest, 0 at top."""
    if len(peers) < 2:
        return 0.5
    lo, hi = peers[0], peers[-1]
    if hi <= lo:
        return 0.5
    return _clamp((hi - price) / (hi - lo), 0.0, 1.0)


def _round_price(value: float) -> float:
    """Round to a tidy menu price (nearest 10 PKR)."""
    return float(round(value / 10.0) * 10)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
