"""Gate the competitive-intelligence eval scenarios in CI.

These run the deterministic agent path against golden scenarios with fixed
expectations, so any drop in the agent's judgement fails the build. See
`evals/competitive_evals.py` for the scenarios and `evals/run.py` for a
human-readable scorecard (plus optional LLM headline grading).
"""

from __future__ import annotations

import pytest

from evals.competitive_evals import SCENARIOS, evaluate


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_eval_scenario_meets_expectations(scenario):
    failures = evaluate(scenario)
    assert not failures, f"{scenario.name} unmet expectations:\n" + "\n".join(failures)


def test_scenarios_are_registered():
    # Guard against an empty/partial scenario set silently passing CI.
    assert len(SCENARIOS) >= 5
