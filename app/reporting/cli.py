#!/usr/bin/env python3
"""Generate the combined HTML audit report from the main dataset.

Pulls together every agent (integrity + retention + operations) into one
owner-facing report. Run from the repo root:

    python -m app.reporting.cli
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.core.ingest import load_dataset
from app.agents.integrity.analyzer import analyze_integrity
from app.agents.retention.analyzer import analyze_retention
from app.agents.operations.analyzer import analyze_operations
from app.reporting.render import generate_report, compute_headlines

DATA = ROOT / "data"
OUT = ROOT / "output"


def main() -> None:
    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )
    integrity = analyze_integrity(orders, menu, staff)
    retention = analyze_retention(orders, menu, staff)
    operations = analyze_operations(orders, menu, staff)

    h = compute_headlines(integrity, retention, operations)
    print(f"Monthly leakage (flagged staff):        PKR {h.monthly_leakage:,.0f}")
    print(f"Monthly revenue:                        PKR {h.monthly_revenue:,.0f}")
    print(f"Recovery rate:                          {h.recovery_rate:.0%}")
    print(f"Potential recoverable/mo (Tier A):       PKR {h.monthly_winback_tier_a:,.0f}")
    print(f"Potential recoverable/mo (A+B total):    PKR {h.monthly_winback_total:,.0f}")
    print(f"Win-back as % of revenue:               {h.winback_pct_of_revenue:.1%}")
    if h.winback_sanity_warning:
        print(f"⚠  WARNING: win-back exceeds 10% of monthly revenue — review assumptions")

    html = generate_report(integrity, retention, operations)

    OUT.mkdir(exist_ok=True)
    out_path = OUT / "audit_report.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"\nReport written to {out_path}")


if __name__ == "__main__":
    main()
