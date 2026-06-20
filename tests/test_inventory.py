"""Tests for the deterministic parts of the inventory management agent.

Tests cover general properties — recipe depletion, preparation rules, and the
stock-status classification. LLM reorder-plan generation is not tested here.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.agents.inventory import (
    build_inventory_status,
    compute_consumption,
    run_inventory_agent,
    sum_receipts,
    was_prepared,
)
from app.models.canonical import (
    Ingredient,
    LineItem,
    MenuItem,
    Order,
    Payment,
    RecipeComponent,
    StockReceipt,
)


# ── Fixtures ──

def _menu() -> dict[str, MenuItem]:
    return {
        "SAN": MenuItem(sku="SAN", name="Chicken Sandwich", category="food", price=1350),
        "CAP": MenuItem(sku="CAP", name="Cappuccino", category="coffee", price=560),
    }


def _ingredients() -> dict[str, Ingredient]:
    return {
        "CHICKEN": Ingredient(ingredient_id="CHICKEN", name="Chicken Portion",
                              unit="piece", unit_cost=220, reorder_level=50),
        "BREAD": Ingredient(ingredient_id="BREAD", name="Bread Slice",
                            unit="slice", unit_cost=25, reorder_level=20),
        "BEAN": Ingredient(ingredient_id="BEAN", name="Coffee Beans",
                           unit="g", unit_cost=3.5, reorder_level=500),
        "MILK": Ingredient(ingredient_id="MILK", name="Milk",
                           unit="ml", unit_cost=0.25, reorder_level=1000),
    }


def _recipes() -> dict[str, list[RecipeComponent]]:
    return {
        "SAN": [
            RecipeComponent(sku="SAN", ingredient_id="CHICKEN", qty_per_unit=1),
            RecipeComponent(sku="SAN", ingredient_id="BREAD", qty_per_unit=2),
        ],
        "CAP": [
            RecipeComponent(sku="CAP", ingredient_id="BEAN", qty_per_unit=18),
            RecipeComponent(sku="CAP", ingredient_id="MILK", qty_per_unit=150),
        ],
    }


def _line(sku: str, qty: int, *, is_void=False, vaf=False, is_comp=False) -> LineItem:
    name = {"SAN": "Chicken Sandwich", "CAP": "Cappuccino"}.get(sku, sku)
    return LineItem(
        item_sku=sku, item_name=name, category="x", qty=qty,
        unit_price=100, line_amount=100 * qty,
        is_void=is_void, void_after_fire=vaf, is_comp=is_comp,
    )


def _order(oid: str, dt_: datetime, lines: list[LineItem]) -> Order:
    return Order(
        order_id=oid, datetime=dt_, staff_id="S1", staff_name="x",
        channel="dine-in", order_status="closed",
        line_items=lines, payments=[Payment(method="cash", amount=100, tax_rate=0.15)],
    )


# ── Preparation rule ──

class TestPreparationRule:
    def test_normal_sale_is_prepared(self):
        assert was_prepared(_line("SAN", 1)) is True

    def test_plain_void_before_fire_not_prepared(self):
        assert was_prepared(_line("SAN", 1, is_void=True, vaf=False)) is False

    def test_void_after_fire_is_prepared(self):
        # cooked then voided -> ingredients were still used (waste)
        assert was_prepared(_line("SAN", 1, is_void=True, vaf=True)) is True

    def test_comp_is_prepared(self):
        # given away free, but still made
        assert was_prepared(_line("SAN", 1, is_comp=True)) is True


# ── Consumption / depletion ──

class TestConsumption:
    def test_chicken_depletes_one_per_sandwich(self):
        """The headline scenario: each sandwich consumes exactly 1 chicken piece."""
        orders = [
            _order("O1", datetime(2026, 5, 1, 12, 0), [_line("SAN", 3)]),
            _order("O2", datetime(2026, 5, 1, 13, 0), [_line("SAN", 2)]),
        ]
        consumed, prepared = compute_consumption(orders, _recipes())
        assert consumed["CHICKEN"] == 5
        assert consumed["BREAD"] == 10  # 2 slices each
        assert prepared["SAN"] == 5

    def test_qty_multiplies_recipe(self):
        orders = [_order("O1", datetime(2026, 5, 1, 9, 0), [_line("CAP", 4)])]
        consumed, _ = compute_consumption(orders, _recipes())
        assert consumed["BEAN"] == 18 * 4
        assert consumed["MILK"] == 150 * 4

    def test_plain_void_consumes_nothing(self):
        orders = [_order("O1", datetime(2026, 5, 1, 9, 0),
                         [_line("SAN", 1, is_void=True, vaf=False)])]
        consumed, prepared = compute_consumption(orders, _recipes())
        assert consumed.get("CHICKEN", 0) == 0
        assert prepared.get("SAN", 0) == 0

    def test_void_after_fire_consumes_ingredients(self):
        orders = [_order("O1", datetime(2026, 5, 1, 9, 0),
                         [_line("SAN", 1, is_void=True, vaf=True)])]
        consumed, _ = compute_consumption(orders, _recipes())
        assert consumed["CHICKEN"] == 1


# ── Receipts ──

class TestReceipts:
    def test_receipts_aggregate_per_ingredient(self):
        receipts = [
            StockReceipt(receipt_id="R1", datetime=datetime(2026, 5, 1),
                         ingredient_id="CHICKEN", qty=250, unit_cost=220),
            StockReceipt(receipt_id="R2", datetime=datetime(2026, 5, 12),
                         ingredient_id="CHICKEN", qty=200, unit_cost=220),
        ]
        totals = sum_receipts(receipts)
        assert totals["CHICKEN"][0] == 450
        assert totals["CHICKEN"][1] == 450 * 220


# ── Status classification ──

class TestStatusClassification:
    def _status_for(self, received: float, consumed: float, reorder: float):
        ings = {"X": Ingredient(ingredient_id="X", name="X", unit="piece",
                                unit_cost=10, reorder_level=reorder)}
        receipts = [StockReceipt(receipt_id="R", datetime=datetime(2026, 5, 1),
                                 ingredient_id="X", qty=received, unit_cost=10)]
        statuses = build_inventory_status(ings, receipts, {"X": consumed}, days_in_period=10)
        return statuses[0]

    def test_healthy_stock_is_ok(self):
        st = self._status_for(received=450, consumed=292, reorder=50)
        assert st.remaining_qty == 158
        assert st.status == "ok"

    def test_below_reorder_level_is_low(self):
        st = self._status_for(received=100, consumed=60, reorder=50)
        assert st.status == "low"

    def test_oversold_when_consumed_exceeds_received(self):
        st = self._status_for(received=300, consumed=334, reorder=50)
        assert st.remaining_qty == -34
        assert st.status == "oversold"

    def test_consumed_cost_uses_unit_cost(self):
        st = self._status_for(received=100, consumed=10, reorder=0)
        assert st.consumed_cost == 100  # 10 units * 10 cost

    def test_usage_rate_and_stockout_projection(self):
        # 50 used over 10 days -> 5/day; 50 remaining -> 10 days to stockout
        st = self._status_for(received=100, consumed=50, reorder=0)
        assert st.usage_per_day == 5
        assert st.days_to_stockout == 10


# ── Unmapped items + end-to-end ──

class TestEndToEnd:
    def test_item_without_recipe_is_flagged_unmapped(self):
        menu = _menu() | {"WTR": MenuItem(sku="WTR", name="Water",
                                          category="other", price=150)}
        orders = [_order("O1", datetime(2026, 5, 1, 9, 0), [_line("WTR", 3)])]
        report = run_inventory_agent(
            orders, menu, _ingredients(), _recipes(), [],
            with_reorder_plan=False,
        )
        unmapped_skus = {u.sku for u in report.unmapped_items}
        assert "WTR" in unmapped_skus

    def test_full_run_produces_balanced_numbers(self):
        orders = [
            _order(f"O{i}", datetime(2026, 5, 1, 12, 0) + timedelta(hours=i),
                   [_line("SAN", 1)])
            for i in range(10)
        ]
        receipts = [StockReceipt(receipt_id="R", datetime=datetime(2026, 5, 1),
                                 ingredient_id="CHICKEN", qty=8, unit_cost=220)]
        report = run_inventory_agent(
            orders, _menu(), _ingredients(), _recipes(), receipts,
            with_reorder_plan=False,
        )
        chicken = next(s for s in report.ingredients if s.ingredient_id == "CHICKEN")
        # 10 sandwiches made, only 8 chicken received -> oversold by 2
        assert chicken.consumed_qty == 10
        assert chicken.remaining_qty == -2
        assert chicken.status == "oversold"
        assert report.days_in_period >= 1
