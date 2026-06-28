"""Internal staff handler — store is already known from the To field.

Staff can write natural-language prompts or shorthand commands. ZAI (glm-4.7)
classifies the intent and routes to the right agent. For natural-language
queries, the raw agent output is also adapted via LLM to directly answer what
was asked.

Explicit action commands (post / edit / ignore) bypass the LLM entirely.
Scout is handled upstream in the gateway as an async background task.
"""
from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "Staff tools — type naturally or use these shortcuts:\n\n"
    "Integrity (POS audit):\n"
    "  summary · leakage · profit · staff · daily · weekly · refresh\n\n"
    "Revenue advisor:\n"
    "  revenue · sales · pricing · strategy\n\n"
    "Competitor scout:\n"
    "  scout  — full competitor intelligence (arrives in 7-10 min)\n\n"
    "Reputation (reviews):\n"
    "  check    — scrape new reviews\n"
    "  post     — publish pending draft reply\n"
    "  edit <text> — revise draft reply\n"
    "  ignore   — skip pending review\n\n"
    "  help  — this message\n"
    "  menu  — switch mode"
)

_REPUTATION_EXACT = {"post", "ignore"}

_INTEGRITY_CMDS = {
    "summary", "audit", "overview",
    "leakage", "leak", "theft", "fraud",
    "profit", "margin", "cogs", "revenue", "sales",
    "staff", "employees", "team",
    "daily", "weekly",
    "refresh", "reload", "update",
    "pdf", "report", "document",
}


def _is_shorthand(text: str) -> bool:
    return len(text.split()) == 1 and text.lower().strip() in (
        _INTEGRITY_CMDS
        | {"check", "scrape", "scout", "competitors", "intel", "help"}
    )


def _classify_with_llm(text: str) -> tuple[str, str]:
    """Return (agent, command) via ZAI. Falls back to keyword routing."""
    try:
        from app.agents.scout.config import ZAI_API_KEY, ZAI_MODEL
        if not ZAI_API_KEY:
            raise RuntimeError("ZAI_API_KEY not set")

        from openai import OpenAI
        client = OpenAI(
            api_key=ZAI_API_KEY,
            base_url="https://open.bigmodel.cn/api/paas/v4/",
        )
        system = (
            "You route WhatsApp messages from restaurant staff to the correct "
            "internal tool. Reply with exactly: agent/command — nothing else.\n\n"
            "Agents and commands:\n"
            "  integrity/summary    overall POS audit executive summary\n"
            "  integrity/leakage    voids, theft, comp abuse, discount abuse\n"
            "  integrity/profit     margins, COGS, gross profit, sales totals\n"
            "  integrity/staff      per-staff anomalies, who had most issues\n"
            "  integrity/daily      today's sales and performance\n"
            "  integrity/weekly     7-day trend comparison\n"
            "  integrity/refresh    re-pull POS data from source\n"
            "  integrity/free_form  any other POS or financial question\n"
            "  revenue/general      sales strategy, growth, upsell, campaigns\n"
            "  reputation/check     scrape and classify new customer reviews now\n"
            "  reputation/chat      questions about review trends or customer feedback\n"
            "  scout/scout          competitor intelligence (only if clearly about competitors)\n"
        )
        resp = client.chat.completions.create(
            model=ZAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=15,
        )
        result = resp.choices[0].message.content.strip().lower()
        if "/" in result:
            agent, command = result.split("/", 1)
            agent = agent.strip()
            command = command.strip()
            if agent in ("integrity", "revenue", "reputation", "scout"):
                logger.info("internal.llm_classify: agent=%s command=%s", agent, command)
                return agent, command
    except Exception as exc:
        logger.warning("internal.llm_classify: failed (%s) — keyword fallback", exc)

    # Keyword fallback
    from app.core.routing import classify_agent
    agent = classify_agent(text) or "integrity"
    first = text.lower().split()[0] if text else ""
    return agent, first


def _adapt_response(original_query: str, raw_response: str) -> str:
    """Reframe the raw agent output as a direct conversational answer."""
    try:
        from app.agents.scout.config import ZAI_API_KEY, ZAI_MODEL
        if not ZAI_API_KEY:
            return raw_response

        from openai import OpenAI
        client = OpenAI(
            api_key=ZAI_API_KEY,
            base_url="https://open.bigmodel.cn/api/paas/v4/",
        )
        resp = client.chat.completions.create(
            model=ZAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "A restaurant manager sent a WhatsApp message. "
                        "You have the system's raw output. "
                        "Rewrite it as a direct, conversational answer to their specific question. "
                        "Keep all numbers and data. "
                        "WhatsApp format: short paragraphs, no markdown headers or bold. "
                        "Do not invent information not in the raw output."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Manager asked: {original_query}\n\nSystem output:\n{raw_response}",
                },
            ],
            temperature=0.3,
            max_tokens=700,
        )
        adapted = resp.choices[0].message.content.strip()
        logger.info("internal.adapt_response: adapted %d chars -> %d chars", len(raw_response), len(adapted))
        return adapted
    except Exception as exc:
        logger.warning("internal.adapt_response: failed (%s) — returning raw", exc)
        return raw_response


def handle_internal_for_store(from_number: str, body: str, store_id: int) -> str:
    """Route one staff message to the right agent. Returns reply text."""
    text = (body or "").strip()

    if not text or text.lower() == "help":
        return HELP_TEXT

    first = text.lower().split()[0]
    is_natural = not _is_shorthand(text)

    # Unambiguous action commands — skip LLM
    if first in _REPUTATION_EXACT:
        logger.info("internal.routing: store=%d agent=reputation trigger=action from=%s", store_id, from_number)
        return _reputation(store_id, from_number, text)

    if first == "edit" and len(text.split()) > 1:
        logger.info("internal.routing: store=%d agent=reputation trigger=edit from=%s", store_id, from_number)
        return _reputation(store_id, from_number, text)

    # LLM classification
    agent, command = _classify_with_llm(text)
    logger.info(
        "internal.routing: store=%d agent=%s command=%s natural=%s from=%s",
        store_id, agent, command, is_natural, from_number,
    )

    if agent == "scout":
        # Scout should have been caught by async dispatch upstream; this is the edge-case fallback.
        return _scout(store_id, from_number, text)

    if agent == "revenue":
        raw = _revenue(store_id, from_number, text)
        return _adapt_response(text, raw) if is_natural else raw

    if agent == "reputation":
        # "check" command → pass "check"; chat → pass original text
        body_to_send = "check" if command == "check" else text
        return _reputation(store_id, from_number, body_to_send)

    # integrity (default)
    # For natural language: pass the LLM-selected command so the right
    # template fires, then adapt the output to answer the original question.
    integrity_body = command if (is_natural and command in _INTEGRITY_CMDS) else text
    raw = _integrity(store_id, from_number, integrity_body)
    return _adapt_response(text, raw) if is_natural else raw


def _integrity(store_id: int, from_number: str, text: str) -> str:
    try:
        from app.agents.integrity.service import get_service
        return get_service().handle_message(store_id, from_number, text)
    except Exception as e:
        logger.warning("internal._integrity: store=%d error=%s", store_id, e)
        return "Integrity agent unavailable right now. Try again shortly."


def _revenue(store_id: int, from_number: str, text: str) -> str:
    try:
        from app.agents.revenue.registry import get_registry
        reply = get_registry().handle(store_id, from_number, text)
        return reply.text
    except Exception as e:
        logger.warning("internal._revenue: store=%d error=%s", store_id, e)
        return "Revenue advisor unavailable right now. Try again shortly."


def _scout(store_id: int, from_number: str, text: str) -> str:
    # Sync fallback — async dispatch in gateway should handle this first.
    return (
        "Kicking off a competitor scan. "
        "Your report will arrive in 7-10 minutes."
    )


def _reputation(store_id: int, from_number: str, text: str) -> str:
    try:
        from app.agents.reputation import process_reputation_owner_reply
        return process_reputation_owner_reply(from_number, text, store_id=store_id)
    except Exception as e:
        logger.warning("internal._reputation: store=%d error=%s", store_id, e)
        return (
            "Reputation agent unavailable right now.\n"
            "Commands: post · edit <text> · ignore · check"
        )
