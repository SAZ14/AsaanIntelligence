#!/usr/bin/env python3
"""Evaluate customer and merchant agents — prints scenario results."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.community_customer import handle_customer_message
from app.agents.community_merchant import handle_merchant_message
from app.api.deps import community_paths, merchant_paths
from app.community.tokens import issue_redeem_code

PHONE = "whatsapp:+923001300001"
OWNER = "whatsapp:+923001234567"


def section(title: str) -> None:
    print(f"\n{'='*60}\n{title}\n{'='*60}")


def say(agent: str, user: str, reply: str) -> None:
    print(f"  [{agent}] Guest/Owner: {user!r}")
    print(f"  → {reply[:200]}{'...' if len(reply) > 200 else ''}")


def main() -> None:
    cp = community_paths()
    mp = merchant_paths()

    section("CUSTOMER AGENT — QR onboarding")
    say("Customer", "Join Sugar Rush!", handle_customer_message(PHONE, "Join Sugar Rush!", **cp).body)
    say("Customer", "Sara Ahmed", handle_customer_message(PHONE, "Sara Ahmed", **cp).body)

    section("CUSTOMER AGENT — Receipt redeem → stamps")
    for i in range(5):
        code = issue_redeem_code(cp["redeem_path"], order_id=f"EVAL-{i}")
        r = handle_customer_message(PHONE, code.code, **cp)
        say("Customer", code.code, r.body)

    section("CUSTOMER AGENT — Returning guest")
    say("Customer", "Hello!", handle_customer_message(PHONE, "Hello!", **cp).body)
    say("Customer", "my stamps", handle_customer_message(PHONE, "my stamps", **cp).body)
    say("Customer", "leaderboard", handle_customer_message(PHONE, "leaderboard", **cp).body)
    say("Customer", "what's new?", handle_customer_message(PHONE, "what's new?", **cp).body)

    section("CUSTOMER AGENT — Edge cases")
    bad = issue_redeem_code(cp["redeem_path"])
    handle_customer_message(PHONE, bad.code, **cp)
    say("Customer", f"{bad.code} (again)", handle_customer_message(PHONE, bad.code, **cp).body)
    say("Customer", "SR-FAKE", handle_customer_message(PHONE, "SR-FAKE", **cp).body)

    section("MERCHANT AGENT — Owner queries")
    say("Merchant", "How many members?", handle_merchant_message(OWNER, "How many members?", **mp).body)
    say("Merchant", "Who is my loyal customer?", handle_merchant_message(OWNER, "Who is my loyal customer?", **mp).body)
    say("Merchant", "What's new on the menu?", handle_merchant_message(OWNER, "What's new on the menu?", **mp).body)
    say("Merchant", "leaderboard", handle_merchant_message(OWNER, "leaderboard", **mp).body)

    section("MERCHANT AGENT — Non-owner blocked")
    say("Merchant", "stats", handle_merchant_message("whatsapp:+923009999999", "stats", **mp).body)

    print("\n✓ Evaluation complete — all scenarios ran without errors.\n")


if __name__ == "__main__":
    main()
