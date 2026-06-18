#!/usr/bin/env python3
"""Run the integrity agent against a restaurant's POS.

Connects through the pluggable POS layer, runs the deterministic audit, layers
the LLM agent on top, and prints an owner-facing report. The LLM steps need
ANTHROPIC_API_KEY; without it the agent still prints the full deterministic
audit (pass --no-llm to skip the LLM explicitly).

Each restaurant is one RestaurantConfig — add more to onboard more venues, each
with its own POS type / connection / field mapping.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pos import build_connector
from app.agents.integrity_agent import run_integrity_agent
from app.venues import RESTAURANTS


def _money(v: float) -> str:
    return f"PKR {v:,.0f}"


def print_report(report) -> None:
    rec = report.reconciliation
    print("=" * 72)
    print(f"INTEGRITY AGENT — {report.venue_name}  ({report.period_days} days)")
    print("=" * 72)

    print("\nEXECUTIVE SUMMARY")
    print("-" * 72)
    print(report.executive_summary)
    print(f"\n[LLM narrative: {'on' if report.llm_used else 'off (deterministic fallback)'}]")

    print("\nPROFIT & PAYMENTS")
    print("-" * 72)
    print(f"  Net sales:            {_money(rec.net_sales)}")
    print(f"  COGS (sold):          {_money(rec.cogs_sold)}")
    print(f"  Gross profit:         {_money(rec.gross_profit)}  ({rec.gross_margin:.1%} margin)")
    print(f"  Wasted COGS:          {_money(rec.wasted_cogs)}  (comped / fired-then-voided)")
    print(f"  Gross collected:      {_money(rec.gross_collected)}  (incl. tax {_money(rec.tax_collected)})")
    print(f"  Books balanced:       {'yes' if rec.books_balanced else 'NO'}")
    print(f"  Payment mismatches:   {rec.payment_mismatch_count}  ({_money(rec.payment_mismatch_abs_value)})")
    print(f"  Tax anomalies:        {rec.tax_anomaly_count}  ({_money(rec.tax_anomaly_value)})")

    print("\n  By payment method:")
    for mb in rec.by_method:
        print(f"    {mb.method:8s}  {mb.orders:>5} orders  {_money(mb.gross_collected):>16}  ({mb.share_pct:.0%})")

    print("\nFINDINGS (impact-ranked)")
    print("-" * 72)
    if not report.findings:
        print("  None — clean books.")
    for f in report.findings:
        print(f"  {f.rank}. [{f.severity.upper():6}] {f.category:18} {f.subject:24} {_money(f.monetary_impact)}")
        print(f"      {f.evidence}")
        if f.recommended_action:
            print(f"      → {f.recommended_action}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the POS integrity agent.")
    parser.add_argument("venue", nargs="?", default="roastery",
                        choices=sorted(RESTAURANTS), help="which restaurant to audit")
    parser.add_argument("--no-llm", action="store_true", help="skip the LLM layer")
    args = parser.parse_args()

    config = RESTAURANTS[args.venue]
    connector = build_connector(config)
    data = connector.fetch()

    report = run_integrity_agent(
        data.orders, data.menu, data.staff,
        venue_name=config.venue_name,
        use_llm=not args.no_llm,
    )
    print_report(report)


if __name__ == "__main__":
    main()
