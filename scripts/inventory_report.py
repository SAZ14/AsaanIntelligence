#!/usr/bin/env python3
"""Run the Inventory Management agent on the main dataset and print the report.

The deterministic stock analysis always runs offline. The LLM reorder plan is
only generated when an API key is available (ANTHROPIC_API_KEY) or --reorder-plan
is passed.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest import load_dataset
from app.ingest.loader import load_ingredients, load_recipes, load_stock_receipts
from app.agents.inventory import run_inventory_agent

DATA = Path(__file__).resolve().parent.parent / "data"

STATUS_LABEL = {
    "oversold": "OVERSOLD",
    "out": "OUT",
    "low": "LOW",
    "ok": "ok",
}


def main() -> None:
    want_plan = "--reorder-plan" in sys.argv or bool(os.environ.get("ANTHROPIC_API_KEY"))

    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )
    ingredients = load_ingredients(DATA / "ingredients.csv")
    recipes = load_recipes(DATA / "recipes.csv")
    receipts = load_stock_receipts(DATA / "stock_receipts.csv")

    print(f"Loaded {len(orders)} orders, {len(ingredients)} ingredients, "
          f"{len(recipes)} recipes, {len(receipts)} deliveries")
    if want_plan:
        print("Running inventory agent (LLM reorder plan ON)...\n")
    else:
        print("Running inventory agent (deterministic only; "
              "pass --reorder-plan or set ANTHROPIC_API_KEY for the LLM plan)...\n")

    report = run_inventory_agent(
        orders, menu, ingredients, recipes, receipts,
        with_reorder_plan=want_plan,
    )

    print("=" * 78)
    print(f"INVENTORY REPORT — {report.venue_name}")
    print("=" * 78)
    print(f"  Period: {report.period_start} → {report.period_end} "
          f"({report.days_in_period} days)")
    print(f"  Theoretical cost of goods used: PKR {report.total_consumed_cost:,.0f}  "
          f"|  Stock received: PKR {report.total_received_cost:,.0f}")

    print("\n" + "=" * 78)
    print("STOCK LEVELS")
    print("=" * 78)
    print(f"  {'ingredient':18}{'recv':>9}{'used':>9}{'left':>9}{'/day':>8}"
          f"{'stockout':>10}  status")
    print("  " + "-" * 74)
    for st in report.ingredients:
        dts = f"{st.days_to_stockout}d" if st.days_to_stockout is not None else "-"
        print(f"  {st.name[:17]:18}{st.received_qty:>9g}{st.consumed_qty:>9g}"
              f"{st.remaining_qty:>9g}{st.usage_per_day:>8g}{dts:>10}  "
              f"{STATUS_LABEL.get(st.status, st.status)}")

    if report.oversold:
        print("\n" + "=" * 78)
        print("OVERSOLD — sold more than received (waste / theft / under-delivery)")
        print("=" * 78)
        for st in report.oversold:
            print(f"  {st.name}: short by {abs(st.remaining_qty):g} {st.unit} "
                  f"(received {st.received_qty:g}, used {st.consumed_qty:g})")

    if report.out_of_stock or report.low_stock:
        print("\n" + "=" * 78)
        print("REORDER ALERTS")
        print("=" * 78)
        for st in report.out_of_stock:
            print(f"  [OUT] {st.name} — fully depleted")
        for st in report.low_stock:
            dts = f", ~{st.days_to_stockout}d left" if st.days_to_stockout else ""
            print(f"  [LOW] {st.name}: {st.remaining_qty:g} {st.unit} left "
                  f"(reorder at {st.reorder_level:g}{dts})")

    if report.unmapped_items:
        print("\n" + "=" * 78)
        print("UNTRACKED MENU ITEMS (no recipe — add one to track stock)")
        print("=" * 78)
        for u in report.unmapped_items:
            print(f"  {u.sku:6} {u.name:25} {u.qty_prepared} prepared")

    if report.reorder_plan:
        print("\n" + "=" * 78)
        print("REORDER PLAN (LLM)")
        print("=" * 78)
        print(report.reorder_plan)


if __name__ == "__main__":
    main()
