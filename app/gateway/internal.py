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
    "*Staff Tools* 🛠\n\n"
    "*POS Audit*\n"
    "summary, leakage, profit, staff, daily, weekly, refresh\n\n"
    "*Revenue Advisor*\n"
    "revenue, sales, pricing, strategy\n\n"
    "*Competitor Scout*\n"
    "scout - full report in 7-10 min 🔍\n\n"
    "*Reviews*\n"
    "check - scrape new reviews\n"
    "post - publish pending draft\n"
    "edit <text> - revise draft\n"
    "ignore - skip current review\n\n"
    "help - this message\n"
    "menu - switch mode"
)

_REPUTATION_EXACT = {"post", "ignore"}

# Documented Revenue Advisor shorthand commands (see HELP_TEXT). These bypass
# the LLM classifier entirely -- bare words like "revenue" and "sales" sound
# financial/POS-related, and the LLM was consistently misrouting them to
# integrity/summary or integrity/free_form instead of revenue/general,
# contradicting the help text. Unambiguous documented commands don't need an
# LLM judgment call, same reasoning as _REPUTATION_EXACT below.
_REVENUE_EXACT = {"revenue", "sales", "pricing", "strategy"}

_INTEGRITY_CMDS = {
    "summary", "audit", "overview",
    "leakage", "leak", "theft", "fraud",
    "profit", "margin", "cogs",
    "staff", "employees", "team",
    "daily", "weekly",
    "refresh", "reload", "update",
    "pdf", "report", "document",
}


def _is_shorthand(text: str) -> bool:
    return len(text.split()) == 1 and text.lower().strip() in (
        _INTEGRITY_CMDS | _REVENUE_EXACT
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

        # FIX: hardcoded system prompt for message router -> store in module or config
        system = (
            "You are a message router for a restaurant management AI.\n"
            "A staff member sent a WhatsApp message — it may be English, Urdu, or Roman Urdu. Route by meaning.\n"
            "Reply with EXACTLY one route in the format agent/command — nothing else, no punctuation.\n\n"
            "ROUTES:\n"
            "integrity/summary   – executive overview, general performance, 'how did we do', full audit\n"
            "integrity/leakage   – theft, voids, comps, discount abuse, missing cash, suspicious transactions\n"
            "integrity/profit    – margins, COGS, cost breakdown, gross profit (NOT growth strategy)\n"
            "integrity/staff     – per-employee anomalies, cashier/waiter breakdown, who had issues\n"
            "integrity/daily     – today's sales, today's revenue, today's performance\n"
            "integrity/weekly    – 7-day trends, this week vs last week, weekly comparison\n"
            "integrity/refresh   – re-sync/reload POS data, fetch latest numbers, update data\n"
            "integrity/free_form – any other POS or financial data question not covered above\n"
            "revenue/general     – growth strategy, upsell tips, campaigns, how to sell more, business advice\n"
            "reputation/check    – SCRAPE new reviews right now: 'check reviews', 'get latest reviews'\n"
            "reputation/chat     – questions ABOUT existing reviews: ratings, complaints, what are customers saying, Google/Foodpanda/Instagram\n"
            "scout/scout         – competitor intelligence, rival restaurants, what competitors are doing\n\n"
            "DISAMBIGUATION RULES (apply these when in doubt):\n"
            "• 'how much did we make/sell today/this week' → integrity/daily or integrity/weekly — POS data query, NOT strategy\n"
            "• 'how to increase sales' / 'grow revenue' / 'new campaign' / 'marketing' → revenue/general\n"
            "• 'check reviews' / 'get new reviews' / 'review lao' / 'koi naye reviews' → reputation/check\n"
            "• 'what are customers saying' / 'bad reviews' / 'our rating' / 'log kya bol rahe' → reputation/chat\n"
            "• mentions competitors / rival restaurants → scout/scout\n"
            "• 'chor' / 'theft' / 'missing money' / 'suspicious' / 'void' → integrity/leakage\n"
            "• 'profit' or 'margin' or 'COGS' → integrity/profit\n"
            "• 'full report' / 'summary' / 'overview' / 'audit' → integrity/summary\n"
            "• 'refresh' / 'reload' / 'update data' → integrity/refresh\n\n"
            "EXAMPLES (message → route):\n"
            "  'aaj kitna hua' → integrity/daily\n"
            "  'is hafte kaisi rahi' → integrity/weekly\n"
            "  'koi chor hai kya' → integrity/leakage\n"
            "  'staff mein koi masla' → integrity/staff\n"
            "  'reviews check karo' → reputation/check\n"
            "  'log kya bol rahe hain' → reputation/chat\n"
            "  'how do we upsell desserts' → revenue/general\n"
            "  'what are competitors offering' → scout/scout\n"
            "  'profit margin kya hai' → integrity/profit\n"
            "  'give me a full summary' → integrity/summary\n"
            "  'data refresh karo' → integrity/refresh\n"
            "  'can you check our google reviews' → reputation/check\n"
            "  'increase karni hai sales' → revenue/general\n"
        )
        resp = client.chat.completions.create(
            model=ZAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=25,
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
                        "A restaurant manager asked a question on WhatsApp. "
                        "You have the system output. "
                        "Rewrite it as a direct, conversational answer to their specific question. "
                        "Keep all numbers and data intact. "
                        "WhatsApp format: short paragraphs, use *word* for bold (single asterisks), "
                        "no markdown headers, no em-dashes, numbered or bullet lists for multiple items. "
                        "Never invent information not in the system output."
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

    if first in _REVENUE_EXACT and len(text.split()) == 1:
        logger.info("internal.routing: store=%d agent=revenue trigger=exact from=%s", store_id, from_number)
        return _revenue(store_id, from_number, text)

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
        return "POS audit is unavailable right now. Please try again shortly."


def _revenue(store_id: int, from_number: str, text: str) -> str:
    try:
        from app.agents.revenue.registry import get_registry
        reply = get_registry().handle(store_id, from_number, text)
        return reply.text
    except Exception as e:
        logger.warning("internal._revenue: store=%d error=%s", store_id, e)
        return "Revenue advisor is unavailable right now. Please try again shortly."


def _scout(store_id: int, from_number: str, text: str) -> str:
    """Fallback for scout-classified messages the upstream keyword-based
    async dispatch in gateway/main.py didn't catch -- e.g. natural-language
    queries like "what are other burger places doing" that contain none of
    _SCOUT_ASYNC_WORDS but still get classified as scout intent by the LLM
    here. This used to just return a static "report coming in 7-10 min"
    message with no pipeline ever running -- confirmed via live testing to
    reliably promise a report that never arrives. Runs the real pipeline
    directly; safe to block here since this only executes inside the
    already-backgrounded _bg_internal task, not the webhook response itself.

    Reuses the same rate-limit (3/hour) and in-flight-run guards as the
    keyword path so this fallback can't be used to bypass them.
    """
    from datetime import datetime, timedelta
    from app.core.db import SessionLocal, ScoutRun as Run
    from app.gateway.main import _scout_rate_ok

    if not _scout_rate_ok(from_number):
        return "You've sent too many scout requests. Limit is 3 per hour — please wait before trying again."

    with SessionLocal() as db:
        cutoff = datetime.utcnow() - timedelta(minutes=15)
        in_flight = db.query(Run).filter(
            Run.store_id == store_id,
            Run.status == "running",
            Run.started_at >= cutoff,
        ).first()
    if in_flight:
        return "Scout is already running — your report will arrive in a few minutes. Please wait."

    try:
        from app.agents.scout.pipeline import run as scout_run
        return scout_run("scout", store_id=store_id, user_message=text)
    except Exception as e:
        logger.error("internal._scout: store=%d error=%s", store_id, e)
        return "Scout report could not be completed. Please try again."


def _reputation(store_id: int, from_number: str, text: str) -> str:
    try:
        from app.agents.reputation import process_reputation_owner_reply
        return process_reputation_owner_reply(from_number, text, store_id=store_id)
    except Exception as e:
        logger.warning("internal._reputation: store=%d error=%s", store_id, e)
        return (
            "Reviews agent is unavailable right now. Please try again shortly.\n\n"
            "Commands: *post* · *edit <text>* · *ignore* · *check*"
        )
