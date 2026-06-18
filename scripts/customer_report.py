#!/usr/bin/env python3
"""Run the Customer Agent — loyalty program, lapse alerts, incentive messages."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest import load_dataset
from app.ingest.loader import load_customers
from app.agents.customer import run_customer_agent, TAGLINE

DATA = Path(__file__).resolve().parent.parent / "data"
VENUE = "Sugar Rush"


def main() -> None:
    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )
    registry = load_customers(DATA / "customers.csv")

    print(f"Loaded {len(orders)} orders, {len(registry)} QR-linked guests")
    print(f"Running Customer Agent...\n")

    report = run_customer_agent(orders, menu, staff, registry, venue_name=VENUE)

    print("=" * 70)
    print(f"CUSTOMER AGENT — {report.venue_name}")
    print(f'"{TAGLINE}"')
    print("=" * 70)
    print(f"  Period:              {report.period_days} days")
    print(f"  Loyalty members:     {report.loyalty.total_members}")
    print(f"  QR-linked:           {report.loyalty.qr_linked_members}")
    print(f"  Active regulars:     {report.loyalty.active_regulars}")
    print(f"  At-risk (lapsing):   {report.loyalty.at_risk_count}")
    print(f"  Lapsed regulars:     {report.loyalty.lapsed_count}")
    print(f"  Win-back at risk:    PKR {report.total_winback_at_risk:,.0f}")
    print(f"  Tiers:               bronze {report.loyalty.tier_bronze} | "
          f"silver {report.loyalty.tier_silver} | "
          f"gold {report.loyalty.tier_gold} | "
          f"platinum {report.loyalty.tier_platinum}")

    print("\n" + "=" * 70)
    print(f"HIGH-VALUE GUESTS (top {len(report.high_value)})")
    print("=" * 70)
    for e in report.high_value[:10]:
        p = e.profile
        qr = "QR" if e.qr_linked else "  "
        print(f"  [{qr}] {e.display_name:18s} {e.segment:12s} {e.loyalty_tier:8s} "
              f"{p.visit_count:3d} visits  PKR {p.total_spend:>9,.0f}  "
              f"{p.days_since_last:2d}d ago")

    if report.corporate_accounts:
        print("\n" + "=" * 70)
        print(f"CORPORATE ACCOUNTS ({len(report.corporate_accounts)})")
        print("=" * 70)
        for e in report.corporate_accounts[:5]:
            p = e.profile
            print(f"  {e.display_name:18s} {p.visit_count} visits  "
                  f"avg PKR {p.avg_ticket:,.0f}  total PKR {p.total_spend:,.0f}")

    if report.banquet_accounts:
        print("\n" + "=" * 70)
        print(f"BANQUET / EVENT ACCOUNTS ({len(report.banquet_accounts)})")
        print("=" * 70)
        for e in report.banquet_accounts[:5]:
            print(f"  {e.display_name:18s} max order PKR {e.order_stats.max_ticket:,.0f}  "
                  f"{e.profile.visit_count} visits")

    print("\n" + "=" * 70)
    print(f"LAPSE ALERTS ({len(report.lapse_alerts)} high-value guests at risk)")
    print("=" * 70)
    for alert in report.lapse_alerts[:12]:
        print(f"\n  [{alert.urgency.upper():8s}] {alert.display_name} ({alert.segment})")
        print(f"  {alert.message}")
        print(f"  Win-back value: PKR {alert.winback_value:,.0f}  |  "
              f"Monthly value: PKR {alert.monthly_value:,.0f}")

    print("\n" + "=" * 70)
    print(f"INCENTIVE MESSAGES ({len(report.incentives)} offers, "
          f"{report.messages_ready} ready to send)")
    print("=" * 70)
    for inc in report.incentives[:15]:
        send = f"→ {inc.channel}" if inc.phone else "→ needs QR link"
        print(f"\n  [{inc.incentive_type}] {inc.display_name}  {send}")
        print(f"  Reward: {inc.reward_text}")
        print(f"  Trigger: {inc.trigger_reason}")
        print(f"  Message: {inc.message[:160]}{'...' if len(inc.message) > 160 else ''}")


if __name__ == "__main__":
    main()
