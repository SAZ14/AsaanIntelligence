#!/usr/bin/env python3
"""Print the full Retention + Operations report for a dataset."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest import load_dataset
from app.analysis.retention import analyze_retention, analyze_operations


def _print_lapse_table(customers, label, limit=15):
    rows = sorted(customers, key=lambda c: c.winback_value, reverse=True)
    print(f"\n  {label} ({len(rows)} customers):")
    hdr = (f"    {'Ref':14s} {'Visits':>6} {'Cadence':>8} {'SinceLast':>10} "
           f"{'Mult':>5} {'AvgTicket':>10} {'Winback':>12}")
    print(hdr)
    print("    " + "-" * (len(hdr) - 4))
    for c in rows[:limit]:
        cad = f"{c.median_cadence_days:.1f}d" if c.median_cadence_days else "n/a"
        print(f"    {c.customer_ref:14s} {c.visit_count:>6} {cad:>8} "
              f"{c.days_since_last:>9}d {c.lapse_multiplier:>5.1f}x "
              f"{c.avg_ticket:>10,.0f} PKR {c.winback_value:>10,.0f}")


def print_report(data_dir: Path, label: str = "") -> None:
    orders, menu, staff = load_dataset(
        data_dir / "sales_detail.csv", data_dir / "menu.csv", data_dir / "staff.csv",
    )
    ret = analyze_retention(orders, menu, staff)
    ops = analyze_operations(orders, menu, staff)

    if label:
        print(f"\n{'#' * 70}")
        print(f"# {label}")
        print(f"{'#' * 70}")

    # ── Retention ──
    print()
    print("=" * 70)
    print("RETENTION")
    print("=" * 70)
    print(f"  Coverage (orders):   {ret.coverage_order_pct:.1%}  ({ret.identified_orders}/{ret.total_orders})")
    print(f"  Coverage (revenue):  {ret.coverage_revenue_pct:.1%}  (PKR {ret.identified_revenue:,.0f}/{ret.total_revenue:,.0f})")
    print(f"  Unique customers:    {ret.unique_customers}")
    print(f"  Repeat customers:    {ret.repeat_customers}  (repeat rate: {ret.repeat_rate:.1%})")
    print(f"  Cadence threshold:   <= {ret.cadence_threshold_days:.1f} days (median of all cadences)")
    print(f"  Regulars:            {ret.regular_count}")

    print()
    print("  TIER A — Lapsed Regulars (high-cadence + lapsing)")
    print(f"    Count:             {ret.lapsed_regular_count}")
    print(f"    Win-back value:    PKR {ret.lapsed_regular_winback:,.0f}")

    print()
    print("  TIER B — All Lapsing Customers (personal cadence breach)")
    print(f"    Count:             {ret.lapsing_count}")
    print(f"    Win-back value:    PKR {ret.lapsing_winback:,.0f}")
    print(f"    Total opportunity: PKR {ret.total_winback_value:,.0f}")

    lapsed_regs = [c for c in ret.customers if c.is_lapsed_regular]
    lapsing_only = [c for c in ret.customers if c.is_lapsing and not c.is_lapsed_regular]
    _print_lapse_table(lapsed_regs, "Tier A — Lapsed Regulars")
    _print_lapse_table(lapsing_only, "Tier B — At-Risk / Lapsing (excl. tier A)")

    # ── Operations ──
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
    root = Path(__file__).resolve().parent.parent
    print_report(root / "data", label="MAIN DATASET")

    holdout = root / "data" / "holdout"
    if holdout.exists():
        print_report(holdout, label="HELD-OUT DATASET")


if __name__ == "__main__":
    main()
