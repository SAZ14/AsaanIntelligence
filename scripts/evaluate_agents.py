#!/usr/bin/env python3
"""Evaluate customer and merchant agents — prints scenario results."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.community_customer import handle_customer_message
from app.agents.community_merchant import handle_merchant_message
from app.api.deps import menu_path, sales_path, staff_path
from app.community.tokens import issue_redeem_code

PHONE = "whatsapp:+923001300001"
OWNER = "whatsapp:+923001234567"


def section(title: str) -> None:
    print(f"\n{'='*60}\n{title}\n{'='*60}")


def say(agent: str, user: str, reply: str) -> None:
    print(f"  [{agent}] Guest/Owner: {user!r}")
    print(f"  → {reply[:200]}{'...' if len(reply) > 200 else ''}")


def main() -> None:
    mp = menu_path()

    section("CUSTOMER AGENT — QR onboarding")
    say("Customer", "Join Sugar Rush!", handle_customer_message(PHONE, "Join Sugar Rush!", menu_path=mp).body)
    say("Customer", "Sara Ahmed", handle_customer_message(PHONE, "Sara Ahmed", menu_path=mp).body)

    section("CUSTOMER AGENT — Receipt redeem → stamps")
    for i in range(5):
        code = issue_redeem_code(order_id=f"EVAL-{i}")
        r = handle_customer_message(PHONE, code.code, menu_path=mp)
        say("Customer", code.code, r.body)

    section("CUSTOMER AGENT — Returning guest")
    say("Customer", "Hello!", handle_customer_message(PHONE, "Hello!", menu_path=mp).body)
    say("Customer", "my stamps", handle_customer_message(PHONE, "my stamps", menu_path=mp).body)
    say("Customer", "leaderboard", handle_customer_message(PHONE, "leaderboard", menu_path=mp).body)
    say("Customer", "what's new?", handle_customer_message(PHONE, "what's new?", menu_path=mp).body)

    section("CUSTOMER AGENT — Edge cases")
    bad = issue_redeem_code()
    handle_customer_message(PHONE, bad.code, menu_path=mp)
    say("Customer", f"{bad.code} (again)", handle_customer_message(PHONE, bad.code, menu_path=mp).body)
    say("Customer", "SR-FAKE", handle_customer_message(PHONE, "SR-FAKE", menu_path=mp).body)

    section("MERCHANT AGENT — Owner queries")
    say("Merchant", "How many members?", handle_merchant_message(OWNER, "How many members?", menu_path=mp, sales_path=sales_path(), staff_path=staff_path()).body)
    say("Merchant", "Who is my loyal customer?", handle_merchant_message(OWNER, "Who is my loyal customer?", menu_path=mp, sales_path=sales_path(), staff_path=staff_path()).body)
    say("Merchant", "What's new on the menu?", handle_merchant_message(OWNER, "What's new on the menu?", menu_path=mp, sales_path=sales_path(), staff_path=staff_path()).body)
    say("Merchant", "leaderboard", handle_merchant_message(OWNER, "leaderboard", menu_path=mp, sales_path=sales_path(), staff_path=staff_path()).body)

    section("MERCHANT AGENT — Non-owner blocked")
    say("Merchant", "stats", handle_merchant_message("whatsapp:+923009999999", "stats", menu_path=mp, sales_path=sales_path(), staff_path=staff_path()).body)

    print("\n✓ Evaluation complete — all scenarios ran without errors.\n")


if __name__ == "__main__":
    main()
