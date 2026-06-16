#!/usr/bin/env python3
"""Print the full IntegrityReport for a dataset."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest import load_dataset
from app.analysis.integrity import analyze_integrity


def print_report(data_dir: Path, label: str = "") -> None:
    orders, menu, staff = load_dataset(
        data_dir / "sales_detail.csv", data_dir / "menu.csv", data_dir / "staff.csv",
    )
    report = analyze_integrity(orders, menu, staff)

    if label:
        print(f"\n{'#' * 70}")
        print(f"# {label}")
        print(f"{'#' * 70}")

    bl = report.venue_baseline
    print()
    print("=" * 70)
    print("VENUE BASELINE")
    print("=" * 70)
    print(f"  Period:             {bl.period_days} days")
    print(f"  Orders:             {bl.total_orders}")
    print(f"  Line items:         {bl.total_lines}")
    print(f"  Gross volume:       PKR {bl.gross_volume:,.0f}")
    print(f"  Void rate:          {bl.void_rate:.2%}  ({bl.void_count} voids, PKR {bl.void_value:,.0f})")
    print(f"  Comp rate:          {bl.comp_rate:.2%}  ({bl.comp_count} comps, PKR {bl.comp_value:,.0f})")
    print(f"  Discount rate:      {bl.discount_rate:.2%}  (PKR {bl.discount_value:,.0f})")
    print(f"  Theft-void rate:    {bl.theft_void_rate:.3%}  (PKR {bl.theft_void_value:,.0f})")

    print()
    print("=" * 70)
    print("STAFF INTEGRITY (worst → best)")
    print("=" * 70)
    header = (
        f"{'ID':<5} {'Name':<10} {'Lines':>6} {'Gross':>10} "
        f"{'Void%':>6} {'Comp%':>6} {'Disc%':>6} {'Cash%':>6} "
        f"{'XTheft':>8} {'XComp':>8} {'XDisc':>8} {'Leak':>8} {'Score':>6}"
    )
    print(header)
    print("-" * len(header))
    for si in report.staff_integrity:
        print(
            f"{si.staff_id:<5} {si.staff_name:<10} {si.total_lines:>6} "
            f"{si.gross_volume:>10,.0f} "
            f"{si.void_rate:>5.1%} {si.comp_rate:>5.1%} {si.discount_rate:>5.1%} "
            f"{si.cash_share:>5.1%} "
            f"{si.excess_theft_void_value:>8,.0f} {si.excess_comp_value:>8,.0f} "
            f"{si.excess_discount_value:>8,.0f} {si.total_leakage:>8,.0f} "
            f"{si.integrity_score:>6.1f}"
        )

    print()
    print("=" * 70)
    print("LEAKAGE SUMMARY (reconciled)")
    print("=" * 70)
    print(f"  Worst offender:             {report.worst_offender}")
    print()
    print(f"  Excess theft voids (period):  PKR {report.suspected_theft_value:>10,.0f}")
    print(f"  Excess comps (period):        PKR {report.excess_comp_value:>10,.0f}")
    print(f"  Excess discounts (period):    PKR {report.excess_discount_value:>10,.0f}")
    print(f"                                    {'─' * 10}")
    component_sum = report.suspected_theft_value + report.excess_comp_value + report.excess_discount_value
    print(f"  Sum of components:            PKR {component_sum:>10,.0f}")
    print(f"  Reported period leakage:      PKR {report.estimated_leakage_period:>10,.0f}")
    assert component_sum == report.estimated_leakage_period, "MISMATCH!"
    print(f"  ✓ components == period total")
    print()
    monthly_check = report.estimated_leakage_period * 30 / bl.period_days
    print(f"  Period × (30/{bl.period_days}):              PKR {monthly_check:>10,.0f}")
    print(f"  Reported monthly leakage:     PKR {report.estimated_leakage_monthly:>10,.0f}")
    assert abs(monthly_check - report.estimated_leakage_monthly) < 1, "MISMATCH!"
    print(f"  ✓ monthly == period × 30/{bl.period_days}")

    print()
    print("=" * 70)
    print(f"TOP FLAGGED EVENTS ({len(report.flagged_events)} total)")
    print("=" * 70)
    for e in report.flagged_events[:15]:
        print(f"  {e.order_id}  {e.staff_id} ({e.staff_name:8s})  "
              f"{e.flag_type:15s}  {e.item_name:20s}  PKR {e.value:,.0f}")


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    print_report(root / "data", label="MAIN DATASET")

    holdout = root / "data" / "holdout"
    if holdout.exists():
        print_report(holdout, label="HELD-OUT DATASET")


if __name__ == "__main__":
    main()
