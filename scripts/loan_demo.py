"""Loans agent demo — scan the customer book and walk through decisions.

Runs fully offline against the deterministic policy engine; if a local LLM is
up (Ollama by default, see app/agents/loans/llm.py), decision narratives come
from the model instead of the built-in templates.

Usage:
    python scripts/loan_demo.py                 # triage the whole book
    python scripts/loan_demo.py --walkthrough   # + narrated deep-dives
    python scripts/loan_demo.py --customer C007 # one customer in detail
    python scripts/loan_demo.py --no-llm        # skip the LLM even if running
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.loans.agent import LoanAgent  # noqa: E402
from app.agents.loans.models import Decision  # noqa: E402

W = 100


def hr(char: str = "─") -> str:
    return char * W


def _rs(x: int | float) -> str:
    return f"Rs {x:,.0f}"


def print_queue_table(title: str, rows: list, show_offer: bool = False) -> None:
    print(f"\n{title}  ({len(rows)})")
    print(hr())
    if not rows:
        print("  (empty)")
        return
    for i, (c, d) in enumerate(rows, 1):
        stress = f"{d.stress.days_below_5k:>2}d<5k"
        trend = ""
        if c.eom_balances:
            trend = f"{_rs(c.eom_balances[0])} → {_rs(c.eom_balances[-1])}"
        line = (
            f"{i:>3}. {c.customer_id}  {c.name:<22} {c.bank_code:<7} "
            f"score {d.score.score:>2} {d.score.band:<10} {stress}  {trend}"
        )
        print(line)
        if show_offer and d.offer:
            o = d.offer
            print(
                f"       ↳ offer {_rs(o.amount)} / {o.tenor_months}mo @ "
                f"{o.annual_rate * 100:.1f}% ({o.rate_type}) — EMI {_rs(o.emi)}, "
                f"DBR {o.dbr_pct}%, headroom after {_rs(o.headroom_after)}/mo"
            )


def print_decision(agent: LoanAgent, cid: str, use_llm: bool) -> None:
    a = agent.assess(cid, use_llm=use_llm)
    c, d = a.customer, a.decision
    print(hr("═"))
    print(f"{c.customer_id}  {c.name}, {c.age} — {c.employment} — {c.bank} ({c.city})")
    print(hr("═"))
    print(f"Income {_rs(c.net_monthly_income)}/mo · account {c.account_age_years}y · "
          f"salary {c.salary_months_12}/12 · avg balance {_rs(c.avg_balance_6m)} · "
          f"obligations {_rs(c.total_obligations)}/mo")
    flags = []
    if c.cheque_bounce_12m:
        flags.append("cheque bounce")
    if c.ecib_dpd90_24m:
        flags.append("eCIB 90+ DPD")
    if c.ecib_writeoff:
        flags.append("eCIB write-off/litigation")
    print(f"Flags: {', '.join(flags) if flags else 'none'}")
    print(f"\nRelationship score: {d.score.score} ({d.score.band})")
    for comp in d.score.components:
        print(f"  {comp.points:+d}  {comp.label}")
    print(f"\nCash stress: {'YES' if d.stress.stressed else 'no'}")
    for note in d.stress.notes:
        print(f"  · {note}")
    print(f"\nDECISION: {d.action}")
    print(f"\nNarrative ({a.narrative_source}):")
    print(a.narrative)
    print()


def pick_walkthrough(queues: dict) -> list[str]:
    """One narrated example per branch: top offer, a liquid monitor, worst decline."""
    picks = []
    if queues["OFFER"]:
        picks.append(queues["OFFER"][0][0].customer_id)
    liquid = [(c, d) for c, d in queues["MONITOR"] if not d.stress.stressed
              and d.score.band in ("STRONG", "ACCEPTABLE")]
    if liquid:
        picks.append(liquid[0][0].customer_id)
    if queues["DECLINE"]:
        picks.append(queues["DECLINE"][0][0].customer_id)
    return picks


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--customer", help="assess one customer id (e.g. C007)")
    ap.add_argument("--walkthrough", action="store_true",
                    help="narrated deep-dive on one customer per queue")
    ap.add_argument("--no-llm", action="store_true",
                    help="deterministic narratives only")
    ap.add_argument("--book", help="path to an alternative customer_book.json")
    args = ap.parse_args()
    use_llm = not args.no_llm

    agent = LoanAgent(book_path=args.book)

    if args.customer:
        print_decision(agent, args.customer, use_llm)
        return

    print(hr("═"))
    print("PROACTIVE LENDING QUEUE — full-book scan".center(W))
    print(("policy: offer only to STRONG/ACCEPTABLE with genuine stress "
           "(10+ days under Rs 5,000); monitor THIN; no unsecured to POOR"
           ).center(W))
    print(hr("═"))

    queues = agent.scan()
    print_queue_table("OFFER (priority order — strongest file, deepest stress first)",
                      queues["OFFER"], show_offer=True)
    print_queue_table("MONITOR", queues["MONITOR"])
    print_queue_table("DECLINE — no unsecured; secured alternatives only",
                      queues["DECLINE"])

    if args.walkthrough:
        print(f"\n\n{'WALKTHROUGHS'.center(W)}\n")
        for cid in pick_walkthrough(queues):
            print_decision(agent, cid, use_llm)


if __name__ == "__main__":
    main()
