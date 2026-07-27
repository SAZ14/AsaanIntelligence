"""Deterministic lending policy engine.

Implements the bank-internal relationship scoring policy, cash-stress
detection, EMI / DBR / affordability math, and the decision gate — all in
code. The LLM never decides; it only explains. Every formula matches the
worked examples in the fine-tuning dataset:

- Relationship score: +2/+1/0 tiers on account age, salary regularity,
  average balance, repayment history; −3 bounce, −4 eCIB 90+ DPD,
  −6 write-off/litigation. Bands: >=6 STRONG | 3-5 ACCEPTABLE |
  0-2 THIN | <0 POOR.
- Genuine cash stress marker: 10+ days under Rs 5,000 in the last 30.
- Proactive offers only to STRONG/ACCEPTABLE showing genuine stress;
  THIN monitored; POOR gets secured alternatives only. DBR cap 40%.
"""
from __future__ import annotations

from app.agents.loans.models import (
    CustomerProfile,
    Decision,
    LoanProduct,
    OfferTerms,
    ScoreBreakdown,
    ScoreComponent,
    StressSignals,
)

DBR_CAP = 0.40
STRESS_DAYS_THRESHOLD = 10   # days under Rs 5,000 in last 30 = genuine stress
ROUND_DOWN_STEP = 10_000     # lendable amounts floor to nearest Rs 10,000

# The demo product shelf. Rates are typical Pakistani unsecured pricing
# (KIBOR + spread); the dataset uses the same shape.
PERSONAL_INSTALMENT_LOAN = LoanProduct(
    name="Personal Instalment Loan",
    min_amount=50_000,
    max_amount=3_000_000,
    tenors=[12, 24, 36, 48],
    annual_rate=0.283,
    rate_type="reducing",
    processing_fee_pct=0.02,
    min_income=50_000,
)

MICROFINANCE_ENTERPRISE_LOAN = LoanProduct(
    name="Microfinance Enterprise Loan",
    min_amount=20_000,
    max_amount=350_000,
    tenors=[6, 12, 18],
    annual_rate=0.383,
    rate_type="flat",
    processing_fee_pct=0.01,
    min_income=15_000,
)


# ── relationship scoring ──

def relationship_score(p: CustomerProfile) -> ScoreBreakdown:
    comps: list[ScoreComponent] = []

    if p.account_age_years >= 3:
        comps.append(ScoreComponent("account age 3 years or more", 2))
    elif p.account_age_years >= 1:
        comps.append(ScoreComponent("account age 1-3 years", 1))
    else:
        comps.append(ScoreComponent("account age under 1 year", 0))

    if p.salary_months_12 >= 11:
        comps.append(ScoreComponent("salary credited 11+ of last 12 months", 2))
    elif p.salary_months_12 >= 9:
        comps.append(ScoreComponent("salary credited 9-10 of last 12 months", 1))
    else:
        comps.append(ScoreComponent("salary credited under 9 of last 12 months", 0))

    if p.avg_balance_6m > 50_000:
        comps.append(ScoreComponent("average balance above Rs 50,000", 2))
    elif p.avg_balance_6m >= 15_000:
        comps.append(ScoreComponent("average balance Rs 15,000-50,000", 1))
    else:
        comps.append(ScoreComponent("average balance below Rs 15,000", 0))

    if p.previous_loan == "clean":
        comps.append(ScoreComponent("previous loan fully repaid, never 30+ days late", 2))
    elif p.previous_loan == "late_1_2":
        comps.append(ScoreComponent("previous loan repaid with 1-2 late months", 1))
    else:
        comps.append(ScoreComponent("no previous loan history", 0))

    if p.cheque_bounce_12m:
        comps.append(ScoreComponent("cheque/direct-debit bounce in last 12 months", -3))
    if p.ecib_dpd90_24m:
        comps.append(ScoreComponent("90+ day delinquency on eCIB in last 24 months", -4))
    if p.ecib_writeoff:
        comps.append(ScoreComponent("write-off/litigation flag on eCIB", -6))

    score = sum(c.points for c in comps)
    if score >= 6:
        band = "STRONG"
    elif score >= 3:
        band = "ACCEPTABLE"
    elif score >= 0:
        band = "THIN"
    else:
        band = "POOR"
    return ScoreBreakdown(components=comps, score=score, band=band)


# ── cash-stress detection ──

def detect_stress(p: CustomerProfile) -> StressSignals:
    notes: list[str] = []
    trend = None
    if len(p.eom_balances) >= 2 and p.eom_balances[0] > 0:
        trend = (p.eom_balances[-1] - p.eom_balances[0]) / p.eom_balances[0] * 100

    stressed = p.days_below_5k_30d >= STRESS_DAYS_THRESHOLD
    if stressed:
        notes.append(
            f"{p.days_below_5k_30d} of the last 30 days under Rs 5,000 "
            f"(stress marker is {STRESS_DAYS_THRESHOLD}+)"
        )
    else:
        notes.append(
            f"only {p.days_below_5k_30d} of the last 30 days under Rs 5,000 "
            f"(below the {STRESS_DAYS_THRESHOLD}-day stress marker)"
        )
    if trend is not None:
        if trend <= -50:
            notes.append(f"end-of-month balances down {abs(trend):.0f}% over 6 months")
        elif trend >= 0:
            notes.append(f"end-of-month balances up {trend:.0f}% over 6 months")
    if p.salary_to_low_gap_days is not None and stressed:
        notes.append(
            f"money typically runs out {p.salary_to_low_gap_days} days before month-end"
        )
    return StressSignals(
        stressed=stressed,
        days_below_5k=p.days_below_5k_30d,
        balance_trend_pct=trend,
        notes=notes,
    )


# ── instalment math ──

def emi(principal: float, annual_rate: float, months: int, rate_type: str) -> float:
    """Exact (unrounded) monthly instalment."""
    if months <= 0:
        raise ValueError(f"Tenor must be positive, got {months}")
    if rate_type == "flat":
        markup = principal * annual_rate * (months / 12)
        return (principal + markup) / months
    r = annual_rate / 12
    if r == 0:
        return principal / months
    growth = (1 + r) ** months
    return principal * r * growth / (growth - 1)


def invert_emi(instalment: float, annual_rate: float, months: int, rate_type: str) -> float:
    """Principal supportable by a given monthly instalment."""
    if months <= 0:
        raise ValueError(f"Tenor must be positive, got {months}")
    if rate_type == "flat":
        return instalment * months / (1 + annual_rate * months / 12)
    r = annual_rate / 12
    if r == 0:
        return instalment * months
    growth = (1 + r) ** months
    return instalment * (growth - 1) / (r * growth)


def dbr(net_income: int, existing_obligations: int, new_emi: float) -> float:
    """Debt burden ratio in percent: (existing + new instalment) / net income."""
    if net_income <= 0:
        return float("inf")
    return (existing_obligations + new_emi) / net_income * 100


def max_affordable_loan(
    p: CustomerProfile, product: LoanProduct, tenor: int, dbr_cap: float = DBR_CAP
) -> int:
    """Largest lendable principal under the DBR cap, floored to Rs 10,000
    and clamped to the product band. Returns 0 if nothing is lendable."""
    headroom = dbr_cap * p.net_monthly_income - p.total_obligations
    if headroom <= 0:
        return 0
    raw = invert_emi(headroom, product.annual_rate, tenor, product.rate_type)
    floored = int(raw // ROUND_DOWN_STEP) * ROUND_DOWN_STEP
    if floored < product.min_amount:
        return 0
    return min(floored, product.max_amount)


def build_offer(
    p: CustomerProfile, product: LoanProduct, tenor: int | None = None
) -> OfferTerms | None:
    """Size a responsible offer at the DBR cap for the given tenor
    (default: the product's longest tenor, which maximises headroom)."""
    tenor = tenor or product.tenors[-1]
    if tenor not in product.tenors:
        raise ValueError(
            f"Tenor {tenor} months not on the shelf for {product.name} "
            f"(available: {product.tenors})"
        )
    amount = max_affordable_loan(p, product, tenor)
    if amount <= 0:
        return None
    exact = emi(amount, product.annual_rate, tenor, product.rate_type)
    return OfferTerms(
        amount=amount,
        tenor_months=tenor,
        annual_rate=product.annual_rate,
        rate_type=product.rate_type,
        emi=round(exact),
        dbr_pct=round(dbr(p.net_monthly_income, p.total_obligations, exact), 1),
        processing_fee=round(amount * product.processing_fee_pct),
        headroom_after=round(p.net_monthly_income - p.total_obligations - exact),
    )


# ── decision gate ──

def _alternatives(p: CustomerProfile) -> list[str]:
    alts = [
        "Deposit-backed finance against any savings held with us",
        "Gold-backed finance — pledged gold typically supports 70-80% of assessed value",
        "Secured card or small nano facility repaid on time for 6-12 months to put "
        "fresh positive lines on eCIB",
    ]
    if p.ecib_writeoff:
        alts.append(
            "If the write-off is disputed, the eCIB correction process through the "
            "reporting bank — a cleaned record reopens the normal shelf"
        )
    return alts


def decide(
    p: CustomerProfile,
    product: LoanProduct = PERSONAL_INSTALMENT_LOAN,
    tenor: int | None = None,
) -> Decision:
    """The policy gate. OFFER only to STRONG/ACCEPTABLE files showing genuine
    cash stress; THIN monitored; POOR declined for unsecured with secured
    alternatives. Enforced here so no model output can override it."""
    score = relationship_score(p)
    stress = detect_stress(p)
    reasons: list[str] = []

    if score.band == "POOR":
        negatives = [c.label for c in score.components if c.points < 0]
        reasons.append(
            f"Score {score.score} (POOR) driven by: " + "; ".join(negatives) + "."
        )
        if stress.stressed:
            reasons.append(
                "The cash stress is real, which makes unsecured lending more "
                "dangerous, not less — it would likely become another delinquency."
            )
        return Decision(
            action="DECLINE", score=score, stress=stress, offer=None,
            reasons=reasons, alternatives=_alternatives(p),
        )

    if score.band == "THIN":
        reasons.append(
            f"Score {score.score} (THIN) — file too thin for a proactive unsecured "
            "offer; keep under monitoring while the relationship builds."
        )
        return Decision(
            action="MONITOR", score=score, stress=stress, offer=None,
            reasons=reasons, alternatives=[],
        )

    # STRONG or ACCEPTABLE from here.
    if not stress.stressed:
        reasons.append(
            f"Score {score.score} ({score.band}) but liquid — no genuine cash "
            "stress, so a proactive offer would land as marketing, not help. "
            "Keep pre-approved."
        )
        return Decision(
            action="MONITOR", score=score, stress=stress, offer=None,
            reasons=reasons, alternatives=[],
        )

    if p.net_monthly_income < product.min_income:
        reasons.append(
            f"Score {score.score} ({score.band}) with genuine stress, but verified "
            f"income Rs {p.net_monthly_income:,} is below the product minimum "
            f"Rs {product.min_income:,}."
        )
        return Decision(
            action="MONITOR", score=score, stress=stress, offer=None,
            reasons=reasons, alternatives=[],
        )

    offer = build_offer(p, product, tenor)
    if offer is None:
        reasons.append(
            f"Score {score.score} ({score.band}) with genuine stress, but existing "
            f"obligations of Rs {p.total_obligations:,}/month leave no instalment "
            f"headroom under the {DBR_CAP:.0%} DBR cap."
        )
        return Decision(
            action="MONITOR", score=score, stress=stress, offer=None,
            reasons=reasons, alternatives=[],
        )

    reasons.append(
        f"Score {score.score} ({score.band}) with genuine cash stress "
        f"({stress.days_below_5k} days under Rs 5,000) — a proactive offer lands "
        "as help. Sized under the 40% DBR cap; offer below the maximum where the "
        "purpose allows, since a customer at exactly the cap has no buffer for a "
        "bad month."
    )
    return Decision(
        action="OFFER", score=score, stress=stress, offer=offer,
        reasons=reasons, alternatives=[],
    )


def triage(
    customers: list[CustomerProfile],
    product: LoanProduct = PERSONAL_INSTALMENT_LOAN,
) -> dict[str, list[tuple[CustomerProfile, Decision]]]:
    """Portfolio triage: OFFER queue prioritised strongest-file-first
    (score desc, then depth of stress), plus MONITOR and DECLINE lists."""
    queues: dict[str, list[tuple[CustomerProfile, Decision]]] = {
        "OFFER": [], "MONITOR": [], "DECLINE": [],
    }
    for c in customers:
        d = decide(c, product)
        queues[d.action].append((c, d))
    queues["OFFER"].sort(key=lambda cd: (-cd[1].score.score, -cd[1].stress.days_below_5k))
    queues["MONITOR"].sort(key=lambda cd: -cd[1].score.score)
    queues["DECLINE"].sort(key=lambda cd: cd[1].score.score)
    return queues
