"""Data models for the loans agent.

Field names and the rendered profile format mirror the fine-tuning dataset
(``data/loans/loans_pakistan_finetune_10k.jsonl``) exactly, so a model
fine-tuned on that data can be dropped in without any prompt changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Obligation:
    label: str
    monthly_amount: int


@dataclass
class CustomerProfile:
    customer_id: str
    name: str
    age: int
    bank: str            # e.g. "Sindh Bank"
    bank_code: str       # e.g. "SNDB"
    city: str
    employment: str      # e.g. "Careem captain (gig)"
    net_monthly_income: int
    account_age_years: float
    salary_months_12: int        # salary/inflow months credited in last 12
    avg_balance_6m: int
    previous_loan: str           # "clean" | "late_1_2" | "none"
    cheque_bounce_12m: bool
    ecib_dpd90_24m: bool         # 90+ DPD on eCIB in last 24 months
    ecib_writeoff: bool          # write-off / litigation flag, ever
    obligations: list[Obligation] = field(default_factory=list)
    eom_balances: list[int] = field(default_factory=list)  # 6 months, oldest first
    days_below_5k_30d: int = 0
    salary_to_low_gap_days: int | None = None  # days before month-end money runs out

    @property
    def total_obligations(self) -> int:
        return sum(o.monthly_amount for o in self.obligations)


@dataclass
class LoanProduct:
    name: str
    min_amount: int
    max_amount: int
    tenors: list[int]            # months
    annual_rate: float           # e.g. 0.283
    rate_type: str               # "reducing" | "flat"
    processing_fee_pct: float    # e.g. 0.02
    min_income: int


@dataclass
class ScoreComponent:
    label: str
    points: int


@dataclass
class ScoreBreakdown:
    components: list[ScoreComponent]
    score: int
    band: str  # STRONG | ACCEPTABLE | THIN | POOR


@dataclass
class StressSignals:
    stressed: bool
    days_below_5k: int
    balance_trend_pct: float | None  # % change first→last of 6 EoM balances
    notes: list[str]


@dataclass
class OfferTerms:
    amount: int
    tenor_months: int
    annual_rate: float
    rate_type: str
    emi: int
    dbr_pct: float               # (existing + new EMI) / net income, in %
    processing_fee: int
    headroom_after: int          # income − obligations − EMI


@dataclass
class Decision:
    action: str                  # OFFER | MONITOR | DECLINE
    score: ScoreBreakdown
    stress: StressSignals
    offer: OfferTerms | None
    reasons: list[str]
    alternatives: list[str]      # for declines: what we can responsibly offer instead
