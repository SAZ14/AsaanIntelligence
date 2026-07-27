"""Dashboard payload + template rendering for the /loans/ui demo page.

One code path serves two builds: the FastAPI route injects a live scan into
the HTML template at request time, and the same function produces the
standalone demo file with the data baked in.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from app.agents.loans.agent import LoanAgent
from app.agents.loans.prompts import template_narrative

TEMPLATE_PATH = Path(__file__).resolve().parent / "static" / "dashboard.html"
DATA_PLACEHOLDER = "/*__LOAN_DATA__*/null"


def build_payload(agent: LoanAgent) -> dict:
    """Everything the dashboard needs: product, queues, and per-customer
    profile + decision + deterministic narrative."""
    queues = agent.scan()
    customers = {}
    order = {"OFFER": [], "MONITOR": [], "DECLINE": []}
    for action, pairs in queues.items():
        for c, d in pairs:
            order[action].append(c.customer_id)
            customers[c.customer_id] = {
                "customer_id": c.customer_id,
                "name": c.name,
                "age": c.age,
                "bank": c.bank,
                "bank_code": c.bank_code,
                "city": c.city,
                "employment": c.employment,
                "net_monthly_income": c.net_monthly_income,
                "account_age_years": c.account_age_years,
                "salary_months_12": c.salary_months_12,
                "avg_balance_6m": c.avg_balance_6m,
                "previous_loan": c.previous_loan,
                "flags": {
                    "bounce": c.cheque_bounce_12m,
                    "dpd90": c.ecib_dpd90_24m,
                    "writeoff": c.ecib_writeoff,
                },
                "obligations": [
                    {"label": o.label, "monthly_amount": o.monthly_amount}
                    for o in c.obligations
                ],
                "total_obligations": c.total_obligations,
                "eom_balances": c.eom_balances,
                "days_below_5k_30d": c.days_below_5k_30d,
                "decision": asdict(d),
                "narrative": template_narrative(c, d),
            }
    p = agent.product
    return {
        "product": {
            "name": p.name,
            "min_amount": p.min_amount,
            "max_amount": p.max_amount,
            "tenors": p.tenors,
            "annual_rate": p.annual_rate,
            "rate_type": p.rate_type,
            "processing_fee_pct": p.processing_fee_pct,
            "min_income": p.min_income,
        },
        "queues": order,
        "customers": customers,
    }


def render_dashboard(agent: LoanAgent) -> str:
    html = TEMPLATE_PATH.read_text()
    payload = json.dumps(build_payload(agent), separators=(",", ":"))
    if DATA_PLACEHOLDER not in html:
        raise RuntimeError("dashboard.html is missing the loan-data placeholder")
    return html.replace(DATA_PLACEHOLDER, payload)
