#!/usr/bin/env python3
"""Print the Customer agent report for a dataset.

Segments, the lapsed list with behavioural descriptors and recovery-adjusted
values, and the recovery-adjusted total. Coverage is stated explicitly:
retention is measured only over carded/wallet customers (~70% of orders);
anonymous cash customers carry no customer_ref and are not recognised.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest import load_dataset
from app.agents.customer import run_from_dataset

DATA = Path(__file__).resolve().parent.parent / "data"


def _print_descriptor_table(rows, limit=15):
    hdr = (f"    {'Ref':14s} {'Visits':>6} {'Cadence':>8} {'SinceLast':>10} "
           f"{'AvgTicket':>11} {'Recoverable/mo':>16}  Top items")
    print(hdr)
    print("    " + "-" * (len(hdr) - 4))
    for d in rows[:limit]:
        cad = f"{d.median_cadence_days:.1f}d" if d.median_cadence_days else "n/a"
        vip = " [VIP]" if d.is_vip else ""
        items = ", ".join(d.top_items[:3])
        print(f"    {d.customer_ref:14s} {d.visit_count:>6} {cad:>8} "
              f"{d.days_since_last:>9}d {d.avg_ticket:>9,.0f} "
              f"{d.recovery_adjusted_value:>13,.0f} PKR  {items}{vip}")


def main() -> None:
    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )
    report = run_from_dataset(orders, menu, staff)

    print("=" * 78)
    print(f"CUSTOMER AGENT REPORT — {report.venue_name}")
    print("=" * 78)
    print(f"  Recognised customers: {report.unique_customers}")
    print(f"  Coverage:             {report.coverage_order_pct:.1%} of orders / "
          f"{report.coverage_revenue_pct:.1%} of revenue carry a customer_ref.")
    print("  NOTE: anonymous cash customers have no customer_ref and are NOT")
    print("        recognised — segments and win-back cover carded/wallet only.")

    seg = report.segments
    print("\n  Segments (frequency tiers partition all recognised customers;")
    print("  VIP is a cross-cutting value tier — top 20% by spend):")
    print(f"    Regulars:    {len(seg.regulars):>4}")
    print(f"    New:         {len(seg.new):>4}")
    print(f"    Occasional:  {len(seg.occasional):>4}")
    print(f"    VIPs:        {len(seg.vips):>4}  (overlaps the tiers above)")

    print("\n" + "=" * 78)
    print(f"LAPSED REGULARS — win-back targets ({len(report.lapsed)} winnable)")
    print("=" * 78)
    print(f"  Lapsed regulars detected (all): {report.lapsed_regular_count}")
    print(f"  Of which lapsed VIPs:           {len(report.lapsed_vips)}")
    _print_descriptor_table(report.lapsed)

    print("\n  Recovery-adjusted opportunity (the ONLY win-back figure reported,")
    print(f"  at {report.recovery_rate:.0%} assumed recovery of observed spend rate):")
    print(f"    Tier A (lapsed regulars):   PKR {report.total_recoverable_monthly:,.0f}/mo")
    print(f"    Tier A+B (incl. at-risk):   PKR {report.total_recoverable_with_at_risk:,.0f}/mo")


if __name__ == "__main__":
    main()
