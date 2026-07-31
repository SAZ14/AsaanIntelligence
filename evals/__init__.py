"""Evals for the Competitive Intelligence agent.

Tests check that code *works*; evals check that the agent's *judgement* holds
to a fixed standard over time. Each scenario is a curated competitive landscape
with explicit expectations — pricing calls, must-detect dishes, momentum
direction, menu gaps, and required headline content. A regression in agent
quality fails the eval even when no test breaks.

The deterministic evals (`SCENARIOS`) run in CI with no API key. An optional
LLM-as-judge grader lives in `evals.run` for local quality spot-checks.
"""

from evals.competitive_evals import SCENARIOS, EvalScenario, evaluate

__all__ = ["SCENARIOS", "EvalScenario", "evaluate"]
