"""Edge-case and invariant tests for the loans agent.

Boundary values on every policy threshold, corrupt-data rejection, LLM-output
sanitisation, and a randomized sweep asserting the invariants that must hold
for any customer the engine will ever see.
"""
from __future__ import annotations

import random

import pytest

from app.agents.loans.agent import LoanAgent, _strip_think
from app.agents.loans.book import profile_from_dict, profile_to_dict
from app.agents.loans.models import CustomerProfile, Obligation
from app.agents.loans.policy import (
    DBR_CAP,
    MICROFINANCE_ENTERPRISE_LOAN,
    PERSONAL_INSTALMENT_LOAN,
    build_offer,
    decide,
    detect_stress,
    emi,
    invert_emi,
    max_affordable_loan,
    relationship_score,
)


def make_profile(**overrides) -> CustomerProfile:
    base = dict(
        customer_id="E001", name="Edge Case", age=35,
        bank="Habib Bank Limited", bank_code="HBL", city="Lahore",
        employment="test (permanent)", net_monthly_income=90_000,
        account_age_years=5.0, salary_months_12=12, avg_balance_6m=120_000,
        previous_loan="clean", cheque_bounce_12m=False,
        ecib_dpd90_24m=False, ecib_writeoff=False, obligations=[],
        eom_balances=[300_000, 250_000, 180_000, 120_000, 60_000, 20_000],
        days_below_5k_30d=18, salary_to_low_gap_days=9,
    )
    base.update(overrides)
    return CustomerProfile(**base)


# ── scoring boundaries (each policy threshold, both sides) ──

@pytest.mark.parametrize("kwargs,component_idx,points", [
    (dict(account_age_years=3.0), 0, 2),    # "3 years or more" is inclusive
    (dict(account_age_years=2.9), 0, 1),
    (dict(account_age_years=1.0), 0, 1),    # "1-3 years" is inclusive at 1
    (dict(account_age_years=0.9), 0, 0),
    (dict(salary_months_12=11), 1, 2),      # "at least 11"
    (dict(salary_months_12=10), 1, 1),
    (dict(salary_months_12=9), 1, 1),
    (dict(salary_months_12=8), 1, 0),
    (dict(avg_balance_6m=50_001), 2, 2),    # "above Rs 50,000" is exclusive
    (dict(avg_balance_6m=50_000), 2, 1),
    (dict(avg_balance_6m=15_000), 2, 1),    # "Rs 15,000-50,000" inclusive at 15k
    (dict(avg_balance_6m=14_999), 2, 0),
])
def test_scoring_thresholds(kwargs, component_idx, points):
    s = relationship_score(make_profile(**kwargs))
    assert s.components[component_idx].points == points


def test_band_boundaries():
    # exactly 0 is THIN, not POOR ("below 0 POOR")
    p = make_profile(account_age_years=5, salary_months_12=8, avg_balance_6m=10_000,
                     previous_loan="late_1_2", cheque_bounce_12m=True)  # 2+0+0+1-3 = 0
    assert relationship_score(p).score == 0
    assert relationship_score(p).band == "THIN"
    # exactly 3 is ACCEPTABLE, exactly 6 is STRONG
    assert relationship_score(make_profile(
        salary_months_12=8, avg_balance_6m=10_000, previous_loan="late_1_2")).band == "ACCEPTABLE"  # 3
    assert relationship_score(make_profile(
        avg_balance_6m=10_000, previous_loan="clean")).band == "STRONG"  # 6


def test_stress_marker_is_inclusive_at_10_days():
    assert detect_stress(make_profile(days_below_5k_30d=10)).stressed
    assert not detect_stress(make_profile(days_below_5k_30d=9)).stressed


# ── instalment math edges ──

def test_emi_rejects_nonpositive_tenor():
    with pytest.raises(ValueError):
        emi(100_000, 0.283, 0, "reducing")
    with pytest.raises(ValueError):
        invert_emi(10_000, 0.283, -6, "flat")


def test_emi_zero_rate_is_simple_division():
    assert emi(120_000, 0.0, 12, "flat") == pytest.approx(10_000)
    assert emi(120_000, 0.0, 12, "reducing") == pytest.approx(10_000)


def test_emi_invert_round_trip():
    for rate_type in ("flat", "reducing"):
        for months in (6, 12, 24, 48):
            inst = emi(500_000, 0.30, months, rate_type)
            assert invert_emi(inst, 0.30, months, rate_type) == pytest.approx(500_000)


def test_off_shelf_tenor_is_rejected():
    with pytest.raises(ValueError, match="not on the shelf"):
        build_offer(make_profile(), PERSONAL_INSTALMENT_LOAN, tenor=60)


# ── affordability edges ──

def test_zero_headroom_and_over_capacity():
    at_cap = make_profile(obligations=[Obligation("x", 36_000)])  # exactly 40% of 90k
    assert max_affordable_loan(at_cap, PERSONAL_INSTALMENT_LOAN, 48) == 0
    over = make_profile(obligations=[Obligation("x", 50_000)])
    assert max_affordable_loan(over, PERSONAL_INSTALMENT_LOAN, 48) == 0
    assert decide(over).action == "MONITOR"


def test_tiny_headroom_below_product_minimum_is_not_lent():
    # Rs 1/month headroom supports far less than the Rs 50,000 product floor
    p = make_profile(obligations=[Obligation("x", 35_999)])
    assert max_affordable_loan(p, PERSONAL_INSTALMENT_LOAN, 48) == 0
    assert decide(p).action == "MONITOR"


def test_huge_income_clamps_to_product_ceiling():
    d = decide(make_profile(net_monthly_income=5_000_000))
    assert d.action == "OFFER"
    assert d.offer.amount == PERSONAL_INSTALMENT_LOAN.max_amount
    assert d.offer.dbr_pct < 40


def test_zero_income_never_offers():
    assert decide(make_profile(net_monthly_income=0)).action == "MONITOR"


def test_flat_product_offer_respects_cap_too():
    p = make_profile(net_monthly_income=30_000)
    d = decide(p, MICROFINANCE_ENTERPRISE_LOAN)
    assert d.action == "OFFER"
    assert d.offer.rate_type == "flat"
    assert d.offer.emi + p.total_obligations <= DBR_CAP * p.net_monthly_income + 1


# ── LLM-output sanitisation ──

def test_strip_think_unclosed_tag_never_leaks_scratchpad():
    assert _strip_think("Answer first. <think>secret reasoning cut off") == "Answer first."
    assert _strip_think("<think>entirely truncated") == ""


def test_strip_think_multiple_blocks():
    assert _strip_think("<think>a</think>X\n<think>b</think>Y") == "X\nY"


def test_empty_llm_reply_falls_back_to_template():
    class TruncatedClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    class R:
                        choices = [type("C", (), {"message": type("M", (), {
                            "content": "<think>ran out of tok"})()})()]
                    return R()
    agent = LoanAgent(customers=[make_profile()], client=TruncatedClient())
    a = agent.assess("E001", use_llm=True)
    assert a.narrative_source == "template"
    assert a.narrative  # never empty


# ── corrupt data is rejected at the book boundary ──

@pytest.mark.parametrize("bad", [
    dict(salary_months_12=15),
    dict(salary_months_12=-1),
    dict(days_below_5k_30d=45),
    dict(days_below_5k_30d=-5),
    dict(net_monthly_income=-100),
    dict(account_age_years=-1.0),
    dict(previous_loan="weird"),
    dict(obligations=[{"label": "x", "monthly_amount": -500}]),
])
def test_book_loader_rejects_corrupt_rows(bad):
    row = profile_to_dict(make_profile())
    row.update(bad)
    with pytest.raises(ValueError):
        profile_from_dict(row)


def test_duplicate_customer_ids_rejected():
    with pytest.raises(ValueError, match="Duplicate customer_id"):
        LoanAgent(customers=[make_profile(), make_profile(name="Impostor")])


def test_unknown_customer_raises_keyerror():
    agent = LoanAgent(customers=[make_profile()])
    with pytest.raises(KeyError):
        agent.get("NOPE")


# ── randomized invariant sweep ──

def test_invariants_hold_for_random_profiles():
    """Whatever the inputs, the gate must never lend unsecured to POOR,
    never breach the DBR cap, and never leave the product band."""
    rng = random.Random(1234)
    actions = set()
    for i in range(500):
        p = make_profile(
            customer_id=f"R{i}",
            net_monthly_income=rng.choice([0, 5_000, 18_000, 50_000, 90_000, 400_000, 2_000_000]),
            account_age_years=round(rng.uniform(0, 20), 1),
            salary_months_12=rng.randint(0, 12),
            avg_balance_6m=rng.choice([0, 5_000, 15_000, 50_000, 51_000, 900_000]),
            previous_loan=rng.choice(["clean", "late_1_2", "none"]),
            cheque_bounce_12m=rng.random() < 0.25,
            ecib_dpd90_24m=rng.random() < 0.2,
            ecib_writeoff=rng.random() < 0.15,
            obligations=[Obligation("o", rng.choice([0, 2_000, 20_000, 80_000]))]
            if rng.random() < 0.6 else [],
            eom_balances=[rng.randint(0, 1_000_000) for _ in range(rng.choice([0, 2, 6]))],
            days_below_5k_30d=rng.randint(0, 30),
            salary_to_low_gap_days=rng.choice([None, 3, 9, 18]),
        )
        d = decide(p)
        actions.add(d.action)
        assert d.action in ("OFFER", "MONITOR", "DECLINE")
        if d.score.band == "POOR":
            assert d.action == "DECLINE" and d.offer is None and d.alternatives
        if d.action == "OFFER":
            o = d.offer
            assert d.score.band in ("STRONG", "ACCEPTABLE")
            assert d.stress.stressed
            assert PERSONAL_INSTALMENT_LOAN.min_amount <= o.amount <= PERSONAL_INSTALMENT_LOAN.max_amount
            assert o.amount % 10_000 == 0
            assert o.emi + p.total_obligations <= DBR_CAP * p.net_monthly_income + 1
            assert o.dbr_pct <= 40.0
            assert o.headroom_after >= 0
        if d.action == "DECLINE":
            assert d.alternatives  # a declined customer always leaves with a path
    assert actions == {"OFFER", "MONITOR", "DECLINE"}  # sweep hit every branch
