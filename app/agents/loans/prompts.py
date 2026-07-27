"""Prompt rendering for the loans agent.

Renders profiles, policy blocks, and questions in *exactly* the format of the
fine-tuning dataset, so the base model sees familiar few-shot prompts today
and the fine-tuned model sees its native training distribution tomorrow.
"""
from __future__ import annotations

from app.agents.loans.models import CustomerProfile, Decision, LoanProduct

SYSTEM_PROMPT = (
    "You are a senior credit officer at a Pakistani bank running the "
    "responsible instant-lending desk. You read customer relationship data, "
    "eCIB records, and cash-flow signals, then walk through lending decisions "
    "under the bank's internal policy. You never recommend unsecured lending "
    "to POOR-band files; you redirect them to secured alternatives. All "
    "amounts are in Pakistani rupees. Be concise, numerate, and direct — a "
    "declined customer should always leave with a workable path."
)

_PREVIOUS_LOAN_TEXT = {
    "clean": "fully repaid, never 30+ days late",
    "late_1_2": "repaid with 1-2 late months",
    "none": "no history",
}


def _rs(x: int | float) -> str:
    return f"Rs {x:,.0f}"


def render_profile(p: CustomerProfile) -> str:
    lines = [
        "CUSTOMER PROFILE",
        f"- Name: {p.name}, {p.age}",
        f"- Bank: {p.bank} ({p.bank_code}), {p.city}",
        f"- Employment: {p.employment}",
        f"- Verified net monthly income: {_rs(p.net_monthly_income)}",
        f"- Account age: {p.account_age_years} years",
        f"- Salary/inflow months (last 12): {p.salary_months_12}/12",
        f"- Average balance (6-month): {_rs(p.avg_balance_6m)}",
        f"- Previous loan: {_PREVIOUS_LOAN_TEXT.get(p.previous_loan, p.previous_loan)}",
        f"- Cheque/DD bounce in last 12 months: {'yes' if p.cheque_bounce_12m else 'no'}",
        f"- eCIB 90+ DPD in last 24 months: {'yes' if p.ecib_dpd90_24m else 'no'}",
        f"- eCIB write-off/litigation flag: {'yes' if p.ecib_writeoff else 'no'}",
    ]
    if p.obligations:
        lines.append("- Existing monthly obligations:")
        for o in p.obligations:
            lines.append(f"    {o.label}: {_rs(o.monthly_amount)}")
        lines.append(f"- Total existing obligations: {_rs(p.total_obligations)}/month")
    else:
        lines.append("- Existing monthly obligations: none")
    if p.eom_balances:
        lines.append(
            "- End-of-month balances (oldest first): "
            + ", ".join(_rs(b) for b in p.eom_balances)
        )
    lines.append(f"- Days below Rs 5,000 in the last 30 days: {p.days_below_5k_30d}")
    if p.salary_to_low_gap_days is not None:
        lines.append(
            "- Average gap between salary credit and balance falling under "
            f"Rs 5,000: {p.salary_to_low_gap_days} days before month-end"
        )
    return "\n".join(lines)


SCORING_POLICY_BLOCK = """RELATIONSHIP SCORING POLICY (bank internal)
+2  account age 3 years or more (+1 if 1-3 years, 0 if under 1 year)
+2  salary/inflow credited in at least 11 of the last 12 months (+1 if 9-10, 0 otherwise)
+2  average balance above Rs 50,000 (+1 if Rs 15,000-50,000, 0 below)
+2  previous loan fully repaid, never 30+ days late (+1 if repaid with 1-2 late months, 0 if no history)
-3  any cheque/direct-debit bounce in the last 12 months (per policy, applied once)
-4  any 90+ day delinquency on eCIB in the last 24 months
-6  any write-off or litigation flag on eCIB, ever
Decision bands: score >= 6 STRONG | 3-5 ACCEPTABLE | 0-2 THIN | below 0 POOR"""


def render_product(product: LoanProduct) -> str:
    rate_kind = "reducing rate" if product.rate_type == "reducing" else "flat rate"
    tenors = "/".join(str(t) for t in product.tenors)
    return (
        f"{product.name}: {_rs(product.min_amount)}-{_rs(product.max_amount)}, "
        f"tenors {tenors} months, {product.annual_rate * 100:.1f}% p.a. ({rate_kind}), "
        f"processing fee {product.processing_fee_pct * 100:.1f}%, "
        f"min income {_rs(product.min_income)}"
    )


def render_offer_question(p: CustomerProfile, product: LoanProduct) -> str:
    return (
        f"{render_profile(p)}\n\n{SCORING_POLICY_BLOCK}\n\n"
        f"Product on the shelf for proactive offers: {render_product(product)}\n"
        "Bank policy: proactive offers only to STRONG or ACCEPTABLE relationships "
        "showing genuine cash stress; THIN gets monitoring; POOR gets no unsecured "
        "offer (secured/deposit-backed alternatives only). DBR cap 40%.\n\n"
        "Should we proactively offer this customer a loan? Walk through the decision."
    )


def render_decision_facts(d: Decision) -> str:
    """The policy engine's verdict, given to the LLM as ground truth to
    narrate. The model explains the decision; it does not make it."""
    lines = [
        "POLICY ENGINE VERDICT (authoritative — do not contradict)",
        f"- Decision: {d.action}",
        f"- Relationship score: {d.score.score} ({d.score.band})",
        "- Score components: "
        + "; ".join(f"{c.label} ({c.points:+d})" for c in d.score.components),
        f"- Cash stress: {'yes' if d.stress.stressed else 'no'} — "
        + "; ".join(d.stress.notes),
    ]
    if d.offer:
        o = d.offer
        lines += [
            f"- Offer: {_rs(o.amount)} over {o.tenor_months} months at "
            f"{o.annual_rate * 100:.1f}% p.a. ({o.rate_type})",
            f"- Instalment: {_rs(o.emi)}/month, DBR {o.dbr_pct}% against the 40% cap",
            f"- Processing fee: {_rs(o.processing_fee)}; monthly headroom after "
            f"all obligations: {_rs(o.headroom_after)}",
        ]
    for r in d.reasons:
        lines.append(f"- Rationale: {r}")
    if d.alternatives:
        lines.append("- Responsible alternatives: " + "; ".join(d.alternatives))
    return "\n".join(lines)


def template_narrative(p: CustomerProfile, d: Decision) -> str:
    """Deterministic fallback narrative when no local LLM is reachable."""
    if d.action == "OFFER" and d.offer:
        o = d.offer
        return (
            f"Offer. {p.name} scores {d.score.score} ({d.score.band}) with genuine "
            f"cash stress ({d.stress.days_below_5k} days under Rs 5,000 in the last "
            f"30) — this is where a proactive offer lands as help rather than "
            f"marketing.\n\nProposed terms: {_rs(o.amount)} over {o.tenor_months} "
            f"months at {o.annual_rate * 100:.1f}% p.a. ({o.rate_type}), instalment "
            f"{_rs(o.emi)}/month, DBR {o.dbr_pct}% against the 40% cap, leaving "
            f"{_rs(o.headroom_after)}/month after all obligations. Offer below the "
            "maximum where the purpose allows — a customer at exactly the cap has "
            "no buffer for a bad month."
        )
    if d.action == "DECLINE":
        alts = "\n".join(f"- {a}" for a in d.alternatives)
        return (
            f"No unsecured offer. {' '.join(d.reasons)}\n\n"
            f"What we can responsibly put in front of them:\n{alts}\n\n"
            "Say it plainly: what blocked approval, that it is repairable, and "
            "which of the above we will open today. A declined customer who leaves "
            "with a workable path stays a customer."
        )
    return f"Monitor. {' '.join(d.reasons)}"
