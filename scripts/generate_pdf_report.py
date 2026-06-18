#!/usr/bin/env python3
"""Generate the phone-friendly PDF audit report for a venue.

    python scripts/generate_pdf_report.py                 # default venue
    python scripts/generate_pdf_report.py roastery --llm  # add LLM summary
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.integrity_agent import run_integrity_agent
from app.pos import build_connector
from app.report.pdf import write_audit_pdf
from app.venues import RESTAURANTS

OUT = Path(__file__).resolve().parent.parent / "output"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the PDF audit report.")
    parser.add_argument("venue", nargs="?", default="roastery", choices=sorted(RESTAURANTS))
    parser.add_argument("--llm", action="store_true", help="include an LLM executive summary")
    args = parser.parse_args()

    config = RESTAURANTS[args.venue]
    data = build_connector(config).fetch()
    report = run_integrity_agent(
        data.orders, data.menu, data.staff,
        venue_name=config.venue_name, use_llm=args.llm,
    )

    out_path = OUT / f"audit_{args.venue}.pdf"
    write_audit_pdf(
        out_path, report.integrity, report.reconciliation,
        venue_name=config.venue_name, summary=report.executive_summary,
    )
    print(f"Net sales:     PKR {report.reconciliation.net_sales:,.0f}")
    print(f"Gross profit:  PKR {report.reconciliation.gross_profit:,.0f} "
          f"({report.reconciliation.gross_margin:.0%})")
    print(f"Monthly leak:  PKR {report.integrity.estimated_leakage_monthly:,.0f}")
    print(f"\nPDF written to {out_path}")


if __name__ == "__main__":
    main()
