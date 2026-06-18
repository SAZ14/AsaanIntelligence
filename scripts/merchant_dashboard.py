#!/usr/bin/env python3
"""Generate merchant Customer Agent HTML dashboard."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.deps import build_merchant_dashboard
from app.report.merchant_render import generate_merchant_dashboard

OUT = Path(__file__).resolve().parent.parent / "output"


def main() -> None:
    dash = build_merchant_dashboard()
    html = generate_merchant_dashboard(dash)

    OUT.mkdir(exist_ok=True)
    out_path = OUT / "merchant_customer.html"
    out_path.write_text(html, encoding="utf-8")

    h = dash.headlines
    print(f"Merchant Customer Agent — {h.venue_name}")
    print(f"  Lapsed: {h.lapsed_count}  At-risk: {h.at_risk_count}")
    print(f"  Pending approval: {h.pending_approval}  Ready to send: {h.ready_to_send}")
    print(f"  Needs QR link: {h.needs_qr_link}")
    print(f"\nDashboard written to {out_path}")


if __name__ == "__main__":
    main()
