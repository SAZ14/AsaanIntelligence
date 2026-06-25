#!/usr/bin/env python3
"""Generate a daily or weekly PDF report for a venue.

    python scripts/generate_period_pdf.py roastery daily
    python scripts/generate_period_pdf.py roastery weekly --llm
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analysis.periodic import build_daily_report, build_weekly_report
from app.pos import build_connector
from app.report.pdf import write_period_pdf
from app.venues import RESTAURANTS

OUT = Path(__file__).resolve().parent.parent / "output"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a daily/weekly PDF report.")
    parser.add_argument("venue", nargs="?", default="roastery", choices=sorted(RESTAURANTS))
    parser.add_argument("kind", nargs="?", default="daily", choices=("daily", "weekly"))
    parser.add_argument("--llm", action="store_true", help="include an LLM executive summary")
    args = parser.parse_args()

    config = RESTAURANTS[args.venue]
    data = build_connector(config).fetch()
    builder = build_daily_report if args.kind == "daily" else build_weekly_report
    period = builder(
        data.orders, data.menu, data.staff,
        venue_name=config.venue_name, use_llm=args.llm,
    )
    if period is None:
        sys.exit("No POS data available to report on.")

    out_path = OUT / f"{args.kind}_{args.venue}.pdf"
    write_period_pdf(out_path, period, summary=period.report.executive_summary if args.llm else "")
    rec = period.report.reconciliation
    print(f"{args.kind.title()} — {period.label}")
    print(f"Net sales:    PKR {rec.net_sales:,.0f}")
    print(f"Gross profit: PKR {rec.gross_profit:,.0f} ({rec.gross_margin:.0%})")
    print(f"Leakage:      PKR {period.report.integrity.estimated_leakage_period:,.0f}")
    print(f"\nPDF written to {out_path}")


if __name__ == "__main__":
    main()
