#!/usr/bin/env python3
"""Print the full RetentionReport for a dataset.

Run from the repo root:   python -m app.agents.retention.cli
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from app.core.ingest import load_dataset
from app.agents.retention.analyzer import analyze_retention


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

    if label:
        print(f"\n{'#' * 70}")
        print(f"# {label}")
        print(f"{'#' * 70}")

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


def main() -> None:
    print_report(ROOT / "data", label="MAIN DATASET")

    holdout = ROOT / "data" / "holdout"
    if holdout.exists():
        print_report(holdout, label="HELD-OUT DATASET")


if __name__ == "__main__":
    main()
