"""Loans agent — instant-loan eligibility, portfolio triage, and proactive offers.

Deterministic policy math lives in :mod:`policy`; the (optionally fine-tuned)
local LLM in :mod:`agent` only narrates and answers free-form questions. The
decision gate is enforced in code, so the model can never approve a file the
policy declines.
"""
from app.agents.loans.agent import LoanAgent, run_loan_agent
from app.agents.loans.models import CustomerProfile, Decision, LoanProduct

__all__ = ["LoanAgent", "run_loan_agent", "CustomerProfile", "Decision", "LoanProduct"]
