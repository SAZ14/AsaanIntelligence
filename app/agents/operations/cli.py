#!/usr/bin/env python3
"""Print the full OperationsReport for a dataset.

Run from the repo root:   python -m app.agents.operations.cli
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.core.ingest import load_dataset
from app.agents.operations.analyzer import analyze_operations


def print_report(data_dir: Path, label: str = "") -> None:
    orders, menu, staff = load_dataset(
        data_dir / "sales_detail.csv", data_dir / "menu.csv", data_dir / "staff.csv",
    )
    ops = analyze_operations(orders, menu, staff)

    if label:
        print(f"\n{'#' * 70}")
        print(f"# {label}")
        print(f"{'#' * 70}")

    print()
    print("=" * 70)
    print("OPERATIONS")
    print("=" * 70)
    print(f"  Avg ticket:          PKR {ops.avg_ticket:,.0f}")
    print(f"  Busiest hour:        {ops.busiest_hour}:00")
    print(f"  Deadest hour:        {ops.deadest_hour}:00")
    print(f"  Busiest day:         {ops.busiest_day}")
    print(f"  Deadest day:         {ops.deadest_day}")
    print(f"  Busiest daypart:     {ops.busiest_daypart}")
    print(f"  Deadest daypart:     {ops.deadest_daypart}")

    print()
    print("  Dayparts:")
    for dp in ops.dayparts:
        print(f"    {dp.name:15s}  {dp.start_hour:02d}–{dp.end_hour:02d}  "
              f"orders={dp.order_count:>5}  rev=PKR {dp.revenue:>10,.0f}  "
              f"avg_ticket=PKR {dp.avg_ticket:>6,.0f}")

    print()
    print("  Days of week:")
    for d in ops.days_of_week:
        print(f"    {d.day_name:10s}  orders={d.order_count:>5}  "
              f"avg/day={d.avg_orders:>5.0f}  rev=PKR {d.revenue:>10,.0f}")

    print()
    print("  Channels:")
    for ch in ops.channels:
        print(f"    {ch.channel:12s}  orders={ch.order_count:>5}  "
              f"rev=PKR {ch.revenue:>10,.0f}  avg_ticket=PKR {ch.avg_ticket:>6,.0f}")

    print()
    print("  Payment shares:")
    for ps in ops.payment_shares:
        print(f"    {ps.method:8s}  {ps.share_pct:>5.1%}  orders={ps.order_count:>5}  "
              f"rev=PKR {ps.revenue:>10,.0f}")

    print()
    print("  Top 10 items by volume:")
    for it in ops.items_by_volume[:10]:
        m = f"{it.margin:.1%}" if it.margin is not None else "n/a"
        print(f"    {it.sku:5s} {it.name:22s}  vol={it.volume:>5}  "
              f"rev=PKR {it.revenue:>10,.0f}  margin={m}")

    print()
    print("  Items by margin (lowest → highest):")
    for it in ops.items_by_margin:
        m = f"{it.margin:.1%}" if it.margin is not None else "n/a"
        tag = " <- DOG" if it.margin is not None and it.margin < 0.30 else ""
        tag = " <- HERO" if it.margin is not None and it.margin > 0.80 else tag
        print(f"    {it.sku:5s} {it.name:22s}  margin={m:>6}  "
              f"vol={it.volume:>5}  rev=PKR {it.revenue:>10,.0f}{tag}")


def main() -> None:
    print_report(ROOT / "data", label="MAIN DATASET")

    holdout = ROOT / "data" / "holdout"
    if holdout.exists():
        print_report(holdout, label="HELD-OUT DATASET")


if __name__ == "__main__":
    main()
