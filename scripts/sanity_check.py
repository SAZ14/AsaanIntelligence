#!/usr/bin/env python3
"""Sanity check: load the dataset and print summary stats."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest import load_dataset

DATA = Path(__file__).resolve().parent.parent / "data"


def main() -> None:
    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv",
        DATA / "menu.csv",
        DATA / "staff.csv",
    )

    total_lines = sum(len(o.line_items) for o in orders)
    revenue = sum(p.amount for o in orders for p in o.payments)
    voids = sum(1 for o in orders for li in o.line_items if li.is_void)
    comps = sum(1 for o in orders for li in o.line_items if li.is_comp)
    custs = {o.customer_ref for o in orders if o.customer_ref}

    digital = sum(1 for o in orders if o.payments[0].method in ("card", "wallet", "qr"))
    cash = sum(1 for o in orders if o.payments[0].method == "cash")
    total = digital + cash
    digital_pct = digital / total * 100 if total else 0

    print(f"Orders:            {len(orders)}")
    print(f"Line items:        {total_lines}")
    print(f"Revenue (PKR):     {revenue:,.0f}")
    print(f"Voids:             {voids}")
    print(f"Comps:             {comps}")
    print(f"Unique customers:  {len(custs)}")
    print(f"Digital share:     {digital_pct:.1f}%  ({digital} digital / {cash} cash)")
    print(f"Menu items:        {len(menu)}")
    print(f"Staff:             {len(staff)}")

    print("\n— Menu margins —")
    for item in sorted(menu.values(), key=lambda x: x.margin or 0, reverse=True):
        print(f"  {item.sku:5s} {item.name:20s}  margin={item.margin:.1%}  cost={item.cost}  price={item.price}")


if __name__ == "__main__":
    main()
