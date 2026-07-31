#!/usr/bin/env python3
"""Run the competitive-intelligence evals and print a scorecard.

  python -m evals.run            # deterministic evals (CI-safe, no API key)
  python -m evals.run --llm      # also grade headline quality with Claude

Exit code is non-zero if any deterministic scenario fails, so this doubles as
a CI gate.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.competitive_evals import SCENARIOS, evaluate, run_scenario


def _deterministic() -> int:
    print("=" * 72)
    print("COMPETITIVE INTELLIGENCE — DETERMINISTIC EVALS")
    print("=" * 72)
    failed = 0
    for sc in SCENARIOS:
        fails = evaluate(sc)
        status = "PASS" if not fails else "FAIL"
        if fails:
            failed += 1
        print(f"\n[{status}] {sc.name}")
        print(f"        {sc.description}")
        for f in fails:
            print(f"        - {f}")
    total = len(SCENARIOS)
    print("\n" + "=" * 72)
    print(f"RESULT: {total - failed}/{total} scenarios passed")
    print("=" * 72)
    return failed


def _llm_grade() -> None:
    """Optional: ask Claude to score headline quality (specific + actionable)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("\n[llm] ANTHROPIC_API_KEY not set — skipping LLM grading.")
        return
    try:
        import anthropic

        client = anthropic.Anthropic()
    except Exception as e:
        print(f"\n[llm] Anthropic unavailable: {e}")
        return

    print("\n" + "=" * 72)
    print("HEADLINE QUALITY (LLM-as-judge, 1-5)")
    print("=" * 72)
    for sc in SCENARIOS:
        report = run_scenario(sc)
        if not report.headlines:
            continue
        headlines = "\n".join(f"- {h}" for h in report.headlines)
        prompt = (
            "You grade competitive-intelligence headlines for a restaurant owner. "
            "Score 1-5 (5 = specific, concrete, actionable; 1 = vague/generic). "
            "Reply with ONLY 'score|one-line reason'.\n\n"
            f"Scenario: {sc.description}\n\nHeadlines:\n{headlines}"
        )
        try:
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=60,
                messages=[{"role": "user", "content": prompt}],
            )
            print(f"  {sc.name}: {resp.content[0].text.strip()}")
        except Exception as e:
            print(f"  {sc.name}: [grading failed: {e}]")


def main() -> None:
    failed = _deterministic()
    if "--llm" in sys.argv:
        _llm_grade()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
