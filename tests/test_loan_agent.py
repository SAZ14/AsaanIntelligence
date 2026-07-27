"""Loans agent tests.

The math tests validate against worked examples taken verbatim from the
fine-tuning dataset, so the deterministic engine and the training data agree.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.loans.agent import LoanAgent, _strip_think
from app.agents.loans.models import CustomerProfile, Obligation
from app.agents.loans.policy import (
    MICROFINANCE_ENTERPRISE_LOAN,
    PERSONAL_INSTALMENT_LOAN,
    dbr,
    decide,
    emi,
    max_affordable_loan,
    relationship_score,
    triage,
)
from app.agents.loans.prompts import render_offer_question, render_profile


def make_profile(**overrides) -> CustomerProfile:
    base = dict(
        customer_id="T001", name="Test Customer", age=35,
        bank="Habib Bank Limited", bank_code="HBL", city="Lahore",
        employment="Careem captain (gig)", net_monthly_income=90_000,
        account_age_years=5.0, salary_months_12=12, avg_balance_6m=120_000,
        previous_loan="clean", cheque_bounce_12m=False,
        ecib_dpd90_24m=False, ecib_writeoff=False, obligations=[],
        eom_balances=[300_000, 250_000, 180_000, 120_000, 60_000, 20_000],
        days_below_5k_30d=18, salary_to_low_gap_days=9,
    )
    base.update(overrides)
    return CustomerProfile(**base)


# ── scoring, validated against the dataset's Zainab Jatoi example ──

def test_relationship_score_matches_dataset_example():
    # Dataset: 7.4y (+2), 11/12 (+2), avg 736,500 (+2), late_1_2 (+1),
    # bounce (−3), write-off (−6) → score −2, POOR
    p = make_profile(
        account_age_years=7.4, salary_months_12=11, avg_balance_6m=736_500,
        previous_loan="late_1_2", cheque_bounce_12m=True, ecib_writeoff=True,
    )
    s = relationship_score(p)
    assert s.score == -2
    assert s.band == "POOR"


@pytest.mark.parametrize("score_kwargs,band", [
    (dict(), "STRONG"),                                        # 2+2+2+2 = 8
    (dict(previous_loan="none", salary_months_12=9), "ACCEPTABLE"),  # 2+1+2+0 = 5
    (dict(account_age_years=0.5, salary_months_12=6,
          avg_balance_6m=10_000, previous_loan="none"), "THIN"),     # 0
    (dict(ecib_dpd90_24m=True, ecib_writeoff=True), "POOR"),         # 8−10 = −2
])
def test_bands(score_kwargs, band):
    assert relationship_score(make_profile(**score_kwargs)).band == band


# ── EMI math, validated against dataset worked examples ──

def test_reducing_emi_matches_dataset_example():
    # Dataset: Rs 490,000 / 36 months / 31.5% reducing → Rs 21,206
    assert round(emi(490_000, 0.315, 36, "reducing")) == 21_206


def test_flat_emi_matches_dataset_example():
    # Dataset: Rs 260,000 / 12 months / 37.2% flat → Rs 29,727 (markup 96,720)
    assert round(emi(260_000, 0.372, 12, "flat")) == 29_727


def test_dbr_matches_dataset_example():
    # Dataset: (0 existing + 21,206 new) on Rs 61,000 → 34.8%
    assert round(dbr(61_000, 0, 21_206), 1) == 34.8


def test_max_affordable_matches_dataset_example():
    # Dataset (Junaid Baloch): income 18,000, obligations 2,000, 40% cap →
    # headroom 5,200; 38.3% flat over 6 months → ~26,185 → floor Rs 20,000
    p = make_profile(
        net_monthly_income=18_000,
        obligations=[Obligation("motorcycle instalment", 2_000)],
    )
    amount = max_affordable_loan(p, MICROFINANCE_ENTERPRISE_LOAN, tenor=6)
    assert amount == 20_000
    inst = emi(20_000, 0.383, 6, "flat")
    assert round(inst) == 3_972
    assert round(dbr(18_000, 2_000, inst), 1) == 33.2


# ── the decision gate ──

def test_poor_file_is_declined_even_under_stress():
    p = make_profile(cheque_bounce_12m=True, ecib_writeoff=True, days_below_5k_30d=25)
    d = decide(p)
    assert d.action == "DECLINE"
    assert d.offer is None
    assert d.alternatives  # always leaves with a workable path
    assert any("eCIB correction" in a for a in d.alternatives)


def test_thin_file_is_monitored():
    p = make_profile(account_age_years=0.5, salary_months_12=6,
                     avg_balance_6m=10_000, previous_loan="none")
    d = decide(p)
    assert d.action == "MONITOR"


def test_liquid_strong_file_is_not_offered():
    p = make_profile(days_below_5k_30d=3,
                     eom_balances=[300_000, 320_000, 340_000, 360_000, 380_000, 400_000])
    d = decide(p)
    assert d.action == "MONITOR"
    assert "pre-approved" in d.reasons[0]


def test_strong_stressed_file_gets_a_capped_offer():
    p = make_profile(obligations=[Obligation("existing personal loan EMI", 6_000)])
    d = decide(p)
    assert d.action == "OFFER"
    assert d.offer is not None
    assert d.offer.dbr_pct <= 40.0
    assert PERSONAL_INSTALMENT_LOAN.min_amount <= d.offer.amount <= PERSONAL_INSTALMENT_LOAN.max_amount
    # instalment + obligations must respect the cap exactly
    assert d.offer.emi + p.total_obligations <= 0.40 * p.net_monthly_income + 1


def test_income_below_product_minimum_blocks_offer():
    p = make_profile(net_monthly_income=30_000)
    d = decide(p)
    assert d.action == "MONITOR"


def test_no_headroom_blocks_offer():
    p = make_profile(obligations=[Obligation("car instalment", 40_000)])
    d = decide(p)  # 40% of 90k = 36k < 40k existing
    assert d.action == "MONITOR"
    assert d.offer is None


# ── triage ordering ──

def test_triage_orders_offers_strongest_first():
    a = make_profile(customer_id="A", days_below_5k_30d=27,
                     previous_loan="late_1_2", salary_months_12=9)   # score 6
    b = make_profile(customer_id="B", days_below_5k_30d=20)          # score 8
    c = make_profile(customer_id="C", days_below_5k_30d=22)          # score 8
    poor = make_profile(customer_id="D", ecib_writeoff=True, ecib_dpd90_24m=True)
    queues = triage([a, b, c, poor])
    offer_ids = [p.customer_id for p, _ in queues["OFFER"]]
    assert offer_ids == ["C", "B", "A"]  # score desc, then deeper stress first
    assert [p.customer_id for p, _ in queues["DECLINE"]] == ["D"]


# ── prompt rendering matches the training-data format ──

def test_render_profile_format():
    text = render_profile(make_profile(
        obligations=[Obligation("motorcycle instalment", 4_500)]))
    assert text.startswith("CUSTOMER PROFILE\n- Name: Test Customer, 35")
    assert "- Bank: Habib Bank Limited (HBL), Lahore" in text
    assert "- Verified net monthly income: Rs 90,000" in text
    assert "- Previous loan: fully repaid, never 30+ days late" in text
    assert "    motorcycle instalment: Rs 4,500" in text
    assert "- Total existing obligations: Rs 4,500/month" in text
    assert "- Days below Rs 5,000 in the last 30 days: 18" in text


def test_render_offer_question_includes_policy_and_product():
    text = render_offer_question(make_profile(), PERSONAL_INSTALMENT_LOAN)
    assert "RELATIONSHIP SCORING POLICY (bank internal)" in text
    assert "Personal Instalment Loan: Rs 50,000-Rs 3,000,000" in text
    assert "DBR cap 40%" in text
    assert text.endswith("Walk through the decision.")


def test_strip_think():
    assert _strip_think("<think>\nsteps\n</think>\n\nAnswer.") == "Answer."


# ── agent + book + API (offline: use_llm=False / narrative template) ──

def test_agent_scans_generated_book():
    from scripts.generate_loan_book import generate
    customers = generate(n=40, seed=7)
    agent = LoanAgent(customers=customers)
    queues = agent.scan()
    assert sum(len(v) for v in queues.values()) == 40
    for _, d in queues["OFFER"]:
        assert d.score.band in ("STRONG", "ACCEPTABLE") and d.stress.stressed
    for _, d in queues["DECLINE"]:
        assert d.score.band == "POOR"


def test_generated_book_is_deterministic():
    from scripts.generate_loan_book import generate
    a, b = generate(n=10, seed=42), generate(n=10, seed=42)
    assert [x.name for x in a] == [x.name for x in b]
    assert [x.eom_balances for x in a] == [x.eom_balances for x in b]


def test_assessment_template_narrative_offline():
    agent = LoanAgent(customers=[make_profile()])
    a = agent.assess("T001", use_llm=False)
    assert a.narrative_source == "template"
    assert a.decision.action == "OFFER"
    assert "Rs" in a.narrative


@pytest.fixture
def client(monkeypatch):
    from scripts.generate_loan_book import generate
    import app.agents.loans.api as loans_api
    monkeypatch.setattr(loans_api, "_agent", LoanAgent(customers=generate(n=20, seed=3)))
    api = FastAPI()
    api.include_router(loans_api.router)
    return TestClient(api)


def test_api_book_and_detail(client):
    rows = client.get("/loans/book").json()["customers"]
    assert len(rows) == 20 and {"score", "band", "stressed"} <= set(rows[0])
    cid = rows[0]["customer_id"]
    detail = client.get(f"/loans/customers/{cid}").json()
    assert detail["profile"]["customer_id"] == cid
    assert detail["score"]["band"] in ("STRONG", "ACCEPTABLE", "THIN", "POOR")
    assert client.get("/loans/customers/NOPE").status_code == 404


def test_api_scan_and_decision(client):
    scan = client.get("/loans/scan").json()
    assert set(scan) == {"OFFER", "MONITOR", "DECLINE"}
    for row in scan["OFFER"]:
        assert row["offer"]["dbr_pct"] <= 40.0
    cid = (scan["OFFER"] or scan["MONITOR"])[0]["customer_id"]
    d = client.get(f"/loans/customers/{cid}/decision").json()
    assert d["decision"]["action"] in ("OFFER", "MONITOR", "DECLINE")
    assert d["narrative_source"] == "template"
