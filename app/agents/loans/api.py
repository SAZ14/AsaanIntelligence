"""FastAPI endpoints for the loans agent (demo).

  GET  /loans/book                          — book summary with scores/bands
  GET  /loans/customers/{cid}               — full profile + score breakdown
  GET  /loans/customers/{cid}/decision      — policy decision (+LLM narrative
                                              with ?narrative=true)
  GET  /loans/scan                          — whole-book triage queues
  POST /loans/ask                           — free-form credit question to the
                                              local model {question, customer_id?}
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.agents.loans.agent import LoanAgent
from app.agents.loans.book import profile_to_dict
from app.agents.loans.models import CustomerProfile, Decision
from app.agents.loans.policy import detect_stress, relationship_score

router = APIRouter(prefix="/loans", tags=["loans"])

_agent: LoanAgent | None = None


def get_agent() -> LoanAgent:
    global _agent
    if _agent is None:
        _agent = LoanAgent()
    return _agent


def _summary_row(c: CustomerProfile, d: Decision | None = None) -> dict:
    score = d.score if d else relationship_score(c)
    stress = d.stress if d else detect_stress(c)
    return {
        "customer_id": c.customer_id,
        "name": c.name,
        "bank": c.bank_code,
        "city": c.city,
        "net_monthly_income": c.net_monthly_income,
        "score": score.score,
        "band": score.band,
        "days_below_5k_30d": stress.days_below_5k,
        "stressed": stress.stressed,
    }


@router.get("/book")
def book_summary() -> dict:
    agent = get_agent()
    return {"customers": [_summary_row(c) for c in agent.customers]}


@router.get("/customers/{cid}")
def customer_detail(cid: str) -> dict:
    agent = get_agent()
    try:
        c = agent.get(cid)
    except KeyError:
        raise HTTPException(404, f"No customer {cid}")
    return {
        "profile": profile_to_dict(c),
        "score": asdict(relationship_score(c)),
        "stress": asdict(detect_stress(c)),
    }


@router.get("/customers/{cid}/decision")
def customer_decision(cid: str, narrative: bool = False) -> dict:
    agent = get_agent()
    try:
        a = agent.assess(cid, use_llm=narrative)
    except KeyError:
        raise HTTPException(404, f"No customer {cid}")
    return {
        "customer_id": cid,
        "decision": asdict(a.decision),
        "narrative": a.narrative,
        "narrative_source": a.narrative_source,
    }


@router.get("/scan")
def scan_book() -> dict:
    agent = get_agent()
    queues = agent.scan()
    out: dict = {}
    for action, pairs in queues.items():
        out[action] = [
            {**_summary_row(c, d),
             "offer": asdict(d.offer) if d.offer else None,
             "reasons": d.reasons,
             "alternatives": d.alternatives}
            for c, d in pairs
        ]
    return out


class AskBody(BaseModel):
    question: str
    customer_id: str | None = None


@router.post("/ask")
def ask(body: AskBody) -> dict:
    agent = get_agent()
    if body.customer_id:
        try:
            agent.get(body.customer_id)
        except KeyError:
            raise HTTPException(404, f"No customer {body.customer_id}")
    answer = agent.ask(body.question, body.customer_id)
    if answer is None:
        raise HTTPException(
            503,
            "Local LLM not reachable — start Ollama (or set LOAN_LLM_BASE_URL) "
            "and try again",
        )
    return {"answer": answer}
