#!/usr/bin/env python3
"""Generate the HTML audit report from the main dataset."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest import load_dataset
from app.analysis.integrity import analyze_integrity
from app.analysis.retention import analyze_retention, analyze_operations
from app.report.render import generate_report, compute_headlines

DATA = Path(__file__).resolve().parent.parent / "data"
OUT = Path(__file__).resolve().parent.parent / "output"


def main() -> None:
    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )
    integrity = analyze_integrity(orders, menu, staff)
    retention = analyze_retention(orders, menu, staff)
    operations = analyze_operations(orders, menu, staff)

    h = compute_headlines(integrity, retention, operations)
    print(f"Monthly leakage (flagged staff): PKR {h.monthly_leakage:,.0f}")
    print(f"Monthly win-back (Tier A):       PKR {h.monthly_winback_tier_a:,.0f}")
    print(f"Monthly win-back (A+B total):    PKR {h.monthly_winback_total:,.0f}")

    html = generate_report(integrity, retention, operations)

    OUT.mkdir(exist_ok=True)
    out_path = OUT / "audit_report.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"\nReport written to {out_path}")


if __name__ == "__main__":
    main()
