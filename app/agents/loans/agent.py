"""The Loans agent.

Scans the customer book, grades every relationship under the bank policy,
detects genuine cash stress, and decides who gets a proactive instant-loan
offer, who is monitored, and who is declined with secured alternatives.

Split of responsibilities (mirrors the Revenue agent design):
  * :mod:`policy` computes every number and makes the decision — deterministic.
  * The local LLM (base model today, fine-tuned tomorrow) narrates the decision
    and answers free-form credit questions. It is handed the policy verdict as
    ground truth and cannot flip it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.agents.loans import llm as loan_llm
from app.agents.loans.book import load_book
from app.agents.loans.models import CustomerProfile, Decision, LoanProduct
from app.agents.loans.policy import PERSONAL_INSTALMENT_LOAN, decide, triage
from app.agents.loans.prompts import (
    SYSTEM_PROMPT,
    render_decision_facts,
    render_offer_question,
    render_profile,
    template_narrative,
)

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def _strip_think(text: str) -> str:
    """The fine-tuned model emits <think> scratchpads; hide them from output.
    An unclosed tag (truncated generation) drops everything from the tag on."""
    text = _THINK_RE.sub("", text)
    if "<think>" in text:
        text = text.split("<think>", 1)[0]
    return text.strip()


@dataclass
class Assessment:
    customer: CustomerProfile
    decision: Decision
    narrative: str
    narrative_source: str  # "llm" | "template"


class LoanAgent:
    def __init__(
        self,
        customers: list[CustomerProfile] | None = None,
        product: LoanProduct | None = None,
        client=None,
        book_path=None,
    ) -> None:
        self.customers = customers if customers is not None else load_book(book_path)
        self.product = product or PERSONAL_INSTALMENT_LOAN
        self.client = client
        self._by_id: dict[str, CustomerProfile] = {}
        for c in self.customers:
            if c.customer_id in self._by_id:
                raise ValueError(f"Duplicate customer_id {c.customer_id!r} in book")
            self._by_id[c.customer_id] = c

    def get(self, customer_id: str) -> CustomerProfile:
        try:
            return self._by_id[customer_id]
        except KeyError:
            raise KeyError(f"No customer {customer_id!r} in the book") from None

    # ── single-customer decision ──

    def assess(self, customer_id: str, use_llm: bool = True) -> Assessment:
        customer = self.get(customer_id)
        decision = decide(customer, self.product)
        narrative, source = None, "template"
        if use_llm:
            prompt = (
                render_offer_question(customer, self.product)
                + "\n\n"
                + render_decision_facts(decision)
            )
            raw = loan_llm.chat(
                prompt,
                system=SYSTEM_PROMPT,
                fewshot_tasks=["proactive_offer_decision", "decline_with_alternatives"],
                client=self.client,
            )
            stripped = _strip_think(raw) if raw else ""
            if stripped:  # empty after stripping (e.g. truncated <think>) → template
                narrative, source = stripped, "llm"
        if narrative is None:
            narrative = template_narrative(customer, decision)
        return Assessment(customer=customer, decision=decision,
                          narrative=narrative, narrative_source=source)

    # ── whole-book scan ──

    def scan(self) -> dict[str, list[tuple[CustomerProfile, Decision]]]:
        """Triage the whole book into OFFER / MONITOR / DECLINE queues,
        offers prioritised strongest-file-deepest-stress first."""
        return triage(self.customers, self.product)

    # ── free-form questions (EMI maths, eCIB reading, restructuring…) ──

    def ask(self, question: str, customer_id: str | None = None) -> str | None:
        """Free-form credit-ops question against the local model, optionally
        grounded in one customer's profile. Returns None if no model is up."""
        content = question
        if customer_id:
            content = f"{render_profile(self.get(customer_id))}\n\n{question}"
        raw = loan_llm.chat(content, system=SYSTEM_PROMPT, client=self.client)
        return _strip_think(raw) if raw else None


# ── convenience one-shot runner (mirrors run_<name>_agent convention) ──

def run_loan_agent(
    customer_ids: list[str] | None = None,
    customers: list[CustomerProfile] | None = None,
    product: LoanProduct | None = None,
    client=None,
    use_llm: bool = True,
) -> list[Assessment]:
    """Assess a list of customers (default: everyone in the book)."""
    agent = LoanAgent(customers=customers, product=product, client=client)
    ids = customer_ids or [c.customer_id for c in agent.customers]
    return [agent.assess(cid, use_llm=use_llm) for cid in ids]
