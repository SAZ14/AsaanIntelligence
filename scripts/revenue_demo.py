"""Offline demo of the Revenue agent against the shipped café dataset.

No server / no Claude key needed (uses the deterministic NLU fallback).

    python -m scripts.revenue_demo
"""

from __future__ import annotations

from datetime import date

from app.revenue.agent import RevenueAgent, render_digest_text
from app.revenue.config import RevenueConfig
from app.revenue.datasource import load_pos

# Pin "as of" to the end of the synthetic dataset so output is reproducible.
AS_OF = date(2026, 5, 31)
OWNER = "+923001234567"


def ask(agent: RevenueAgent, q: str) -> None:
    print(f"\n  Owner ▶  {q}")
    reply = agent.handle_message(OWNER, q)
    for line in reply.text.splitlines():
        print(f"  Revenue ◀  {line}")


def main() -> None:
    orders, menu, staff = load_pos()
    agent = RevenueAgent(orders=orders, menu=menu, staff=staff,
                         config=RevenueConfig(), client=None, as_of=AS_OF)

    print("=" * 72)
    print("Owner chats with the Revenue agent over WhatsApp")
    print("=" * 72)
    ask(agent, "How did we do this week?")
    ask(agent, "What are the best sellers this month?")
    ask(agent, "How do I grow revenue?")
    ask(agent, "How do I raise the average ticket?")
    ask(agent, "Give me menu advice")
    ask(agent, "How do I get more repeat customers?")
    ask(agent, "What can I raise prices on?")
    ask(agent, "When are we slow?")
    ask(agent, "Give me some campaign ideas to fill quiet times")
    ask(agent, "Send me a weekly digest")

    print("\n" + "=" * 72)
    print("Example scheduled WEEKLY digest that would be pushed to the owner:")
    print("=" * 72)
    digest = agent.build_owner_digest("week")
    print(render_digest_text(digest, agent.config.venue_name))


if __name__ == "__main__":
    main()
