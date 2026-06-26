"""Revenue-growth strategy engine.

Turns POS data into concrete, owner-facing advice across the proven café
revenue levers:

  1. Average transaction value — food-attach upsell + bundle/combo ideas
  2. Customer frequency      — loyalty programme + win-back of lapsed regulars
  3. Daypart / space         — fill dead windows with events / extended hours
  4. Menu optimisation       — feature high-margin heroes, fix the "dogs",
                               add missing high-margin categories

Everything is grounded in the venue's own numbers and, where possible, carries a
conservative monthly PKR impact estimate. This agent advises the owner only — it
does not contact customers.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from app.analysis.retention import DAY_NAMES, analyze_operations, analyze_retention
from app.models.canonical import MenuItem, Order, Staff
from app.agents.revenue.config import RevenueConfig


# ── dataclasses ──

@dataclass
class AttachInsight:
    beverage_orders: int
    attach_rate: float                 # share of drink orders that also buy food
    avg_food_price: float
    target_rate: float
    est_monthly_uplift: float
    top_bundle: str = ""               # e.g. "Cappuccino + Butter Croissant"


@dataclass
class MenuMove:
    kind: str                          # "feature" | "fix" | "add"
    name: str
    detail: str


@dataclass
class FrequencyInsight:
    repeat_rate: float
    regulars: int
    lapsed_regulars: int
    winback_value: float
    loyalty_note: str = ""


@dataclass
class StrategyItem:
    lever: str                         # "Average ticket" | "Frequency" | "Dayparts" | "Menu"
    title: str
    action: str
    rationale: str
    est_monthly_impact: float | None = None


@dataclass
class StrategyPlaybook:
    headline: str                      # tailored intro (peak time + top seller)
    items: list[StrategyItem] = field(default_factory=list)
    attach: AttachInsight | None = None
    menu_moves: list[MenuMove] = field(default_factory=list)
    frequency: FrequencyInsight | None = None


# ── 1. Average ticket: food attach + bundles ──

def analyze_attach(
    orders: list[Order],
    menu: dict[str, MenuItem],
    config: RevenueConfig,
    period_days: int = 30,
) -> AttachInsight:
    bev = config.beverage_categories
    exclude = config.addon_exclude_categories

    bev_orders = 0
    bev_with_food = 0
    food_unit_prices: list[float] = []
    pair_counts: Counter[tuple[str, str]] = Counter()

    for o in orders:
        live = [li for li in o.line_items if not li.is_void and not li.is_comp]
        bev_items = [li for li in live if li.category in bev]
        food_items = [li for li in live
                      if li.category not in bev and li.category not in exclude]
        if bev_items:
            bev_orders += 1
            if food_items:
                bev_with_food += 1
                # remember the most common drink+food pairing for a combo idea
                pair_counts[(bev_items[0].item_name, food_items[0].item_name)] += 1
        for li in food_items:
            if li.qty:
                food_unit_prices.append(li.line_amount / li.qty)

    attach_rate = bev_with_food / bev_orders if bev_orders else 0.0
    avg_food_price = (
        statistics.mean(food_unit_prices) if food_unit_prices
        else _avg_food_menu_price(menu, bev, exclude)
    )
    target = min(attach_rate + config.target_attach_uplift, 0.85)
    delta = max(target - attach_rate, 0.0)
    monthly_bev = bev_orders * (30.0 / max(period_days, 1))
    uplift = delta * monthly_bev * avg_food_price * config.attach_capture_factor

    top_bundle = ""
    if pair_counts:
        (drink, food), _ = pair_counts.most_common(1)[0]
        top_bundle = f"{drink} + {food}"

    return AttachInsight(
        beverage_orders=bev_orders,
        attach_rate=round(attach_rate, 3),
        avg_food_price=round(avg_food_price, 0),
        target_rate=round(target, 3),
        est_monthly_uplift=round(uplift, 0),
        top_bundle=top_bundle,
    )


def _avg_food_menu_price(menu, bev, exclude) -> float:
    prices = [mi.price for mi in menu.values()
              if mi.category not in bev and mi.category not in exclude]
    return statistics.mean(prices) if prices else 0.0


# ── 4. Menu optimisation ──

def menu_opportunities(
    orders: list[Order],
    menu: dict[str, MenuItem],
    staff: dict[str, Staff],
    config: RevenueConfig,
) -> list[MenuMove]:
    ops = analyze_operations(orders, menu, staff)
    sold = {r.sku: r for r in ops.items_by_volume if r.volume > 0}
    moves: list[MenuMove] = []

    # Feature the highest-margin items that already sell — push them harder.
    priced = [mi for mi in menu.values() if mi.margin is not None]
    heroes = sorted(
        [mi for mi in priced if mi.sku in sold],
        key=lambda mi: mi.margin, reverse=True,
    )[:2]
    for mi in heroes:
        moves.append(MenuMove(
            kind="feature", name=mi.name,
            detail=f"{mi.margin*100:.0f}% margin and already selling — "
                   f"put it on specials / barista upsell.",
        ))

    # Fix or cut the low-margin "dogs" that still take up menu space.
    dogs = sorted(
        [mi for mi in priced if mi.sku in sold],
        key=lambda mi: mi.margin,
    )[:1]
    for mi in dogs:
        if mi.margin is not None and mi.margin < 0.35:
            moves.append(MenuMove(
                kind="fix", name=mi.name,
                detail=f"only {mi.margin*100:.0f}% margin — reprice, re-source, "
                       f"or drop it for something that earns its place.",
            ))

    # Suggest high-margin categories the menu is missing.
    present = {mi.category for mi in menu.values()}
    for cat in config.recommended_categories:
        if cat.split()[0].rstrip("s").lower() not in {p.lower() for p in present}:
            moves.append(MenuMove(
                kind="add", name=cat,
                detail="high-margin category you don't carry yet — easy add to "
                       "lift both ticket size and choice.",
            ))
    return moves


# ── 2. Frequency / loyalty ──

def frequency_opportunities(
    orders: list[Order],
    menu: dict[str, MenuItem],
    staff: dict[str, Staff],
    config: RevenueConfig,
) -> FrequencyInsight:
    rep = analyze_retention(orders, menu, staff)
    note = (f"Buy-{config.loyalty_punch_target}-get-1 card: with {rep.regular_count} "
            f"regulars, even +{config.extra_visits_per_regular:g} visit/mo each is real money.")
    return FrequencyInsight(
        repeat_rate=round(rep.repeat_rate, 3),
        regulars=rep.regular_count,
        lapsed_regulars=rep.lapsed_regular_count,
        winback_value=round(rep.lapsed_regular_winback, 0),
        loyalty_note=note,
    )


# ── The playbook (ties the levers together, prioritised) ──

def build_playbook(
    orders: list[Order],
    menu: dict[str, MenuItem],
    staff: dict[str, Staff],
    config: RevenueConfig | None = None,
    period_days: int = 30,
) -> StrategyPlaybook:
    config = config or RevenueConfig()
    ops = analyze_operations(orders, menu, staff)
    attach = analyze_attach(orders, menu, config, period_days)
    moves = menu_opportunities(orders, menu, staff, config)
    freq = frequency_opportunities(orders, menu, staff, config)

    top_seller = ops.items_by_volume[0].name if ops.items_by_volume else "your best seller"
    headline = (f"Peak trade is {ops.busiest_daypart.lower()} and {top_seller} leads sales. "
                f"Here's where the upside is:")

    items: list[StrategyItem] = []

    # Lever 1 — average ticket via attach + bundle
    if attach.beverage_orders:
        bundle = f" Bundle “{attach.top_bundle}” as a combo." if attach.top_bundle else ""
        items.append(StrategyItem(
            lever="Average ticket",
            title="Lift food-attach on drink orders",
            action=(f"Only {attach.attach_rate*100:.0f}% of drink orders add food. "
                    f"Train baristas to suggest a pastry / add-on (syrup, dairy "
                    f"alternative) → aim for {attach.target_rate*100:.0f}%.{bundle}"),
            rationale=f"{attach.beverage_orders} drink orders in window; "
                      f"avg food item ≈ PKR {attach.avg_food_price:,.0f}.",
            est_monthly_impact=attach.est_monthly_uplift,
        ))

    # Lever 2 — frequency via loyalty + win-back
    items.append(StrategyItem(
        lever="Frequency",
        title="Loyalty card + win back lapsed regulars",
        action=(f"Launch a buy-{config.loyalty_punch_target}-get-1 card; "
                f"win back {freq.lapsed_regulars} lapsed regulars with a "
                f"members-only invite."),
        rationale=f"Repeat rate {freq.repeat_rate*100:.0f}%, {freq.regulars} regulars, "
                  f"lapsed-regular value ≈ PKR {freq.winback_value:,.0f}.",
        est_monthly_impact=freq.winback_value or None,
    ))

    # Lever 3 — dayparts / space: fill the deadest daypart
    dead_dp = next((d for d in ops.dayparts if d.name == ops.deadest_daypart), None)
    if dead_dp:
        active = [d for d in ops.dayparts if d.order_count > 0]
        median_rev = statistics.median([d.revenue for d in active]) if active else 0.0
        gap = max(median_rev - dead_dp.revenue, 0.0)
        items.append(StrategyItem(
            lever="Dayparts",
            title=f"Fill the {ops.deadest_daypart.lower()} lull",
            action=("Host acoustic nights / open-mic / private bookings, or run an "
                    "evening small-plates + craft-drinks menu to pull a new crowd."),
            rationale=f"{ops.deadest_daypart} is your quietest daypart vs a "
                      f"{ops.busiest_daypart.lower()} peak.",
            est_monthly_impact=round(gap, 0) if gap else None,
        ))

    # Lever 4 — menu optimisation
    if moves:
        feature = next((m for m in moves if m.kind == "feature"), None)
        fix = next((m for m in moves if m.kind == "fix"), None)
        add = next((m for m in moves if m.kind == "add"), None)
        bits = []
        if feature:
            bits.append(f"feature {feature.name}")
        if fix:
            bits.append(f"fix/cut {fix.name}")
        if add:
            bits.append(f"add {add.name}")
        items.append(StrategyItem(
            lever="Menu",
            title="Optimise the menu mix",
            action=("Push high-margin heroes, fix the low-margin laggard, fill a "
                    f"category gap: {', '.join(bits)}."),
            rationale="Margins computed from cost vs price across the live menu.",
            est_monthly_impact=None,
        ))

    # Prioritise by modelled impact (unknowns last).
    items.sort(key=lambda i: (i.est_monthly_impact is None, -(i.est_monthly_impact or 0)))

    return StrategyPlaybook(
        headline=headline, items=items, attach=attach,
        menu_moves=moves, frequency=freq,
    )

