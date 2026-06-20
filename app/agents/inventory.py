"""Inventory Management agent.

Connects a venue's recipes (bill of materials) to its POS sales and answers
the operational question: *what stock do we have left, and what runs out next?*

Core idea
---------
Every menu item (SKU) has a recipe — a list of ingredients with a quantity per
unit. e.g. Chicken Sandwich (SAN) -> 1 piece of CHICKEN. The agent:

  1. sums each ingredient delivered (StockReceipt deliveries),
  2. replays the POS sales line items, depleting ingredients per recipe,
  3. reports remaining stock, low-stock / out-of-stock alerts, theoretical
     cost of goods sold, and how many days until each ingredient runs out.

Preparation rule
----------------
An item consumes ingredients only if it was actually made. We reuse the POS
adjustment flags already on each LineItem:

  * a plain void (rung then cancelled BEFORE the kitchen fired it) consumes
    nothing — it was never prepared;
  * a void_after_fire WAS cooked, so it still consumes ingredients (waste);
  * a comp (given away free) was made, so it consumes ingredients too.

The deterministic engine below is fully testable. The optional LLM layer only
drafts a human-readable reorder plan; it never changes the numbers.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import anthropic

from app.models.canonical import (
    Ingredient,
    LineItem,
    MenuItem,
    Order,
    RecipeComponent,
    StockReceipt,
)


DEFAULT_VENUE_NAME = "Sugar Rush"


# ── Data classes ──

@dataclass
class IngredientStatus:
    ingredient_id: str
    name: str
    unit: str
    received_qty: float = 0.0
    consumed_qty: float = 0.0
    remaining_qty: float = 0.0
    received_cost: float = 0.0
    consumed_cost: float = 0.0  # theoretical cost of goods sold for this item
    reorder_level: float = 0.0
    usage_per_day: float = 0.0
    days_to_stockout: float | None = None  # None = no usage / never depletes
    depletion_pct: float = 0.0  # share of received stock already used
    status: str = "ok"  # "ok", "low", "out", "oversold"


@dataclass
class UnmappedItem:
    """A menu item that sold but has no recipe — its usage can't be tracked."""
    sku: str
    name: str
    qty_prepared: int = 0


@dataclass
class InventoryReport:
    venue_name: str = ""
    period_start: str = ""
    period_end: str = ""
    days_in_period: int = 0
    ingredients: list[IngredientStatus] = field(default_factory=list)
    unmapped_items: list[UnmappedItem] = field(default_factory=list)
    total_received_cost: float = 0.0
    total_consumed_cost: float = 0.0  # theoretical COGS over the period
    reorder_plan: str = ""

    @property
    def low_stock(self) -> list[IngredientStatus]:
        return [i for i in self.ingredients if i.status == "low"]

    @property
    def out_of_stock(self) -> list[IngredientStatus]:
        return [i for i in self.ingredients if i.status == "out"]

    @property
    def oversold(self) -> list[IngredientStatus]:
        return [i for i in self.ingredients if i.status == "oversold"]


# ── Preparation rule ──

def was_prepared(li: LineItem) -> bool:
    """True if the line item was actually made, and so consumed ingredients."""
    # A plain void before the kitchen fired it was never prepared.
    if li.is_void and not li.void_after_fire:
        return False
    return True


# ── Deterministic depletion engine ──

def receipt_base_qty(
    receipt: StockReceipt,
    ingredients: dict[str, Ingredient] | None = None,
) -> float:
    """Convert a delivery's qty into the ingredient's base (recipe) unit.

    A receipt recorded in the ingredient's pack_unit (e.g. 500 litres) is
    multiplied by pack_size to get base units (500 * 1000 = 500000 ml).
    Anything else (blank unit, or already the base unit) is taken as-is.
    """
    ing = ingredients.get(receipt.ingredient_id) if ingredients else None
    if ing and receipt.unit and receipt.unit == ing.pack_unit and ing.pack_size:
        return receipt.qty * ing.pack_size
    return receipt.qty


def sum_receipts(
    receipts: list[StockReceipt],
    ingredients: dict[str, Ingredient] | None = None,
) -> dict[str, tuple[float, float]]:
    """Aggregate deliveries per ingredient -> (total_base_qty, total_cost).

    Quantities are normalised to the ingredient's base unit; cost is taken as
    qty * unit_cost (unit_cost is per the receipt's own unit, so the total is
    correct regardless of whether the delivery was in packs or base units).
    """
    totals: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for r in receipts:
        totals[r.ingredient_id][0] += receipt_base_qty(r, ingredients)
        if r.unit_cost is not None:
            totals[r.ingredient_id][1] += r.qty * r.unit_cost
    return {k: (v[0], v[1]) for k, v in totals.items()}


def compute_consumption(
    orders: list[Order],
    recipes: dict[str, list[RecipeComponent]],
) -> tuple[dict[str, float], dict[str, int]]:
    """Replay sales -> (qty consumed per ingredient, qty prepared per SKU).

    The per-SKU prepared counts are returned so callers can surface menu items
    that sold but have no recipe (untrackable).
    """
    consumed: dict[str, float] = defaultdict(float)
    prepared_per_sku: dict[str, int] = defaultdict(int)

    for order in orders:
        for li in order.line_items:
            if not was_prepared(li):
                continue
            prepared_per_sku[li.item_sku] += li.qty
            for comp in recipes.get(li.item_sku, []):
                consumed[comp.ingredient_id] += comp.qty_per_unit * li.qty

    return dict(consumed), dict(prepared_per_sku)


def _period_days(orders: list[Order]) -> tuple[str, str, int]:
    if not orders:
        return "", "", 0
    times = [o.datetime for o in orders]
    start, end = min(times), max(times)
    days = (end.date() - start.date()).days + 1
    return str(start.date()), str(end.date()), max(days, 1)


def build_inventory_status(
    ingredients: dict[str, Ingredient],
    receipts: list[StockReceipt],
    consumed: dict[str, float],
    days_in_period: int,
) -> list[IngredientStatus]:
    received = sum_receipts(receipts, ingredients)
    statuses: list[IngredientStatus] = []

    # Cover every ingredient we know about, plus any that only appear in
    # receipts or consumption (defensive — keeps unexpected ids visible).
    ids = set(ingredients) | set(received) | set(consumed)

    for iid in sorted(ids):
        ing = ingredients.get(iid)
        rec_qty, rec_cost = received.get(iid, (0.0, 0.0))
        used = consumed.get(iid, 0.0)
        remaining = rec_qty - used
        unit_cost = ing.unit_cost if ing and ing.unit_cost is not None else None

        st = IngredientStatus(
            ingredient_id=iid,
            name=ing.name if ing else iid,
            unit=ing.unit if ing else "",
            received_qty=round(rec_qty, 3),
            consumed_qty=round(used, 3),
            remaining_qty=round(remaining, 3),
            received_cost=round(rec_cost, 2),
            consumed_cost=round(used * unit_cost, 2) if unit_cost is not None else 0.0,
            reorder_level=ing.reorder_level if ing else 0.0,
        )

        st.usage_per_day = round(used / days_in_period, 3) if days_in_period else 0.0
        if rec_qty > 0:
            st.depletion_pct = round(min(used / rec_qty, 1.0) * 100, 1)
        if st.usage_per_day > 0 and remaining > 0:
            st.days_to_stockout = round(remaining / st.usage_per_day, 1)

        if remaining < 0:
            st.status = "oversold"  # sold more than was received -> waste/theft/under-receipt
        elif remaining <= 0:
            st.status = "out"
        elif remaining <= st.reorder_level:
            st.status = "low"
        else:
            st.status = "ok"

        statuses.append(st)

    return statuses


def find_unmapped_items(
    prepared_per_sku: dict[str, int],
    recipes: dict[str, list[RecipeComponent]],
    menu: dict[str, MenuItem],
) -> list[UnmappedItem]:
    unmapped = [
        UnmappedItem(
            sku=sku,
            name=menu[sku].name if sku in menu else sku,
            qty_prepared=qty,
        )
        for sku, qty in prepared_per_sku.items()
        if sku not in recipes
    ]
    unmapped.sort(key=lambda u: u.qty_prepared, reverse=True)
    return unmapped


# ── Optional LLM layer: human-readable reorder plan ──

def _alerts_summary(report: InventoryReport) -> str:
    lines: list[str] = []
    for st in report.oversold:
        lines.append(
            f"- OVERSOLD: {st.name} short by {abs(st.remaining_qty):g} {st.unit} "
            f"(received {st.received_qty:g}, used {st.consumed_qty:g})"
        )
    for st in report.out_of_stock:
        lines.append(f"- OUT: {st.name} ({st.unit}) fully depleted")
    for st in report.low_stock:
        dts = f", ~{st.days_to_stockout}d left" if st.days_to_stockout else ""
        lines.append(
            f"- LOW: {st.name} {st.remaining_qty:g} {st.unit} left "
            f"(reorder at {st.reorder_level:g}{dts}); uses ~{st.usage_per_day:g}/day"
        )
    return "\n".join(lines) if lines else "No alerts — all ingredients above reorder level."


def generate_reorder_plan(
    report: InventoryReport,
    client: anthropic.Anthropic,
    venue_name: str = DEFAULT_VENUE_NAME,
) -> str:
    prompt = f"""You are the inventory manager for {venue_name}, a café/restaurant.
Below are stock alerts from replaying {report.days_in_period} days of POS sales
against current recipes and deliveries.

{_alerts_summary(report)}

Write a short, practical reorder briefing for the owner (max ~6 bullet points):
- which ingredients to reorder now and a rough quantity to cover ~14 days,
- call out any OVERSOLD item as a likely waste/theft/under-delivery issue to investigate,
- keep it concrete and concise. No emojis."""

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:  # pragma: no cover - network/credentials dependent
        return f"[reorder plan generation failed: {e}]"


# ── Main agent ──

def run_inventory_agent(
    orders: list[Order],
    menu: dict[str, MenuItem],
    ingredients: dict[str, Ingredient],
    recipes: dict[str, list[RecipeComponent]],
    receipts: list[StockReceipt],
    venue_name: str = DEFAULT_VENUE_NAME,
    client: anthropic.Anthropic | None = None,
    with_reorder_plan: bool = True,
) -> InventoryReport:
    start, end, days = _period_days(orders)
    consumed, prepared_per_sku = compute_consumption(orders, recipes)
    statuses = build_inventory_status(ingredients, receipts, consumed, days)
    unmapped = find_unmapped_items(prepared_per_sku, recipes, menu)

    report = InventoryReport(
        venue_name=venue_name,
        period_start=start,
        period_end=end,
        days_in_period=days,
        ingredients=statuses,
        unmapped_items=unmapped,
        total_received_cost=round(sum(s.received_cost for s in statuses), 2),
        total_consumed_cost=round(sum(s.consumed_cost for s in statuses), 2),
    )

    if with_reorder_plan:
        if client is None:
            client = anthropic.Anthropic()
        report.reorder_plan = generate_reorder_plan(report, client, venue_name)

    return report
