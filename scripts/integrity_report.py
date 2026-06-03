#!/usr/bin/env python3
"""Print the full IntegrityReport for the synthetic dataset."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest import load_dataset
from app.analysis.integrity import analyze_integrity

DATA = Path(__file__).resolve().parent.parent / "data"


def main() -> None:
    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )
    report = analyze_integrity(orders, menu, staff)

    bl = report.venue_baseline
    print("=" * 70)
    print("VENUE BASELINE")
    print("=" * 70)
    print(f"  Period:          {bl.period_days} days")
    print(f"  Orders:          {bl.total_orders}")
    print(f"  Line items:      {bl.total_lines}")
    print(f"  Gross volume:    PKR {bl.gross_volume:,.0f}")
    print(f"  Void rate:       {bl.void_rate:.2%}  ({bl.void_count} voids, PKR {bl.void_value:,.0f})")
    print(f"  Comp rate:       {bl.comp_rate:.2%}  ({bl.comp_count} comps, PKR {bl.comp_value:,.0f})")
    print(f"  Discount rate:   {bl.discount_rate:.2%}  (PKR {bl.discount_value:,.0f})")

    print()
    print("=" * 70)
    print("STAFF INTEGRITY (worst → best)")
    print("=" * 70)
    header = (
        f"{'ID':<5} {'Name':<10} {'Lines':>6} {'Gross':>10} "
        f"{'Void%':>6} {'Comp%':>6} {'Disc%':>6} {'Cash%':>6} "
        f"{'Theft':>8} {'XComp':>8} {'XDisc':>8} {'Leak':>8} {'Score':>6}"
    )
    print(header)
    print("-" * len(header))
    for si in report.staff_integrity:
        print(
            f"{si.staff_id:<5} {si.staff_name:<10} {si.total_lines:>6} "
            f"{si.gross_volume:>10,.0f} "
            f"{si.void_rate:>5.1%} {si.comp_rate:>5.1%} {si.discount_rate:>5.1%} "
            f"{si.cash_share:>5.1%} "
            f"{si.theft_void_value:>8,.0f} {si.excess_comp_value:>8,.0f} "
            f"{si.excess_discount_value:>8,.0f} {si.total_leakage:>8,.0f} "
            f"{si.integrity_score:>6.1f}"
        )

    print()
    print("=" * 70)
    print("LEAKAGE SUMMARY")
    print("=" * 70)
    print(f"  Worst offender:           {report.worst_offender}")
    print(f"  Suspected theft (period): PKR {report.suspected_theft_value:,.0f}")
    print(f"  Excess comps (period):    PKR {report.excess_comp_value:,.0f}")
    print(f"  Excess discounts (period):PKR {report.excess_discount_value:,.0f}")
    print(f"  Total leakage (period):   PKR {report.estimated_leakage_period:,.0f}")
    print(f"  Total leakage (monthly):  PKR {report.estimated_leakage_monthly:,.0f}")

    print()
    print("=" * 70)
    print(f"TOP FLAGGED EVENTS ({len(report.flagged_events)} total)")
    print("=" * 70)
    for e in report.flagged_events[:15]:
        print(f"  {e.order_id}  {e.staff_id} ({e.staff_name:8s})  "
              f"{e.flag_type:15s}  {e.item_name:20s}  PKR {e.value:,.0f}")


if __name__ == "__main__":
    main()
