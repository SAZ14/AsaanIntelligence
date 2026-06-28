"""Internal staff handler — store is already known from the To field.

Keyword routing is transparent to the user: they just type naturally and the
server figures out which agent to call. No agent-selection step needed.

Keywords:
  scout / competitors / intel          → competitor scout
  integrity / audit / leakage / profit
  / staff / void / daily / weekly      → integrity agent
  revenue / sales / pricing / strategy → revenue agent
  post / edit / ignore / check
  / review / reviews / feedback        → reputation agent
  menu / back / home                   → return to mode-selection screen
  help                                 → show available commands
"""
from __future__ import annotations
import logging

from app.core.routing import classify_agent

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "Staff tools\n\n"
    "Integrity (POS audit):\n"
    "  summary · audit · leakage · profit · staff · daily · weekly · refresh\n\n"
    "Revenue advisor:\n"
    "  revenue · sales · pricing · strategy · upsell\n\n"
    "Competitor scout:\n"
    "  scout  — triggers a live competitor report\n\n"
    "Reputation (reviews):\n"
    "  check  — scrape new reviews\n"
    "  post   — publish pending draft reply\n"
    "  edit <text> — revise draft reply\n"
    "  ignore — skip pending review\n\n"
    "  help   — this message\n"
    "  menu   — switch between staff tools and customer app"
)

_SCOUT_TRIGGERS = {"scout", "competitors", "competitor", "intel"}

# Explicit reputation commands that take priority over other routing
_REPUTATION_EXACT = {"post", "ignore"}
_REPUTATION_WORDS = {"review", "reviews", "feedback", "rating", "ratings", "check", "scrape"}


def handle_internal_for_store(from_number: str, body: str, store_id: int) -> str:
    """Route one staff message to the right internal agent. Returns reply text."""
    import re as _re

    text = (body or "").strip()

    if not text or text.lower() == "help":
        return HELP_TEXT

    first = text.lower().split()[0]
    text_lower = text.lower()
    words = set(_re.sub(r"[^\w\s]", "", text_lower).split())

    # Explicit reputation commands (unambiguous)
    if first in _REPUTATION_EXACT:
        logger.info("internal.routing: store=%d agent=reputation trigger=exact_cmd from=%s", store_id, from_number)
        return _reputation(store_id, from_number, text)

    if first == "edit" and len(text.split()) > 1:
        logger.info("internal.routing: store=%d agent=reputation trigger=edit from=%s", store_id, from_number)
        return _reputation(store_id, from_number, text)

    if first in _SCOUT_TRIGGERS:
        logger.info("internal.routing: store=%d agent=scout trigger=keyword from=%s", store_id, from_number)
        return _scout(store_id, from_number, text)

    agent = classify_agent(text)
    if agent == "scout":
        logger.info("internal.routing: store=%d agent=scout trigger=classify from=%s", store_id, from_number)
        return _scout(store_id, from_number, text)
    if agent == "revenue":
        logger.info("internal.routing: store=%d agent=revenue trigger=classify from=%s", store_id, from_number)
        return _revenue(store_id, from_number, text)

    # Review/reputation keyword check (before defaulting to integrity)
    if words & _REPUTATION_WORDS:
        logger.info("internal.routing: store=%d agent=reputation trigger=keyword from=%s", store_id, from_number)
        return _reputation(store_id, from_number, text)

    # Default — integrity handles the widest range of internal queries
    logger.info("internal.routing: store=%d agent=integrity trigger=default from=%s", store_id, from_number)
    return _integrity(store_id, from_number, text)


def _integrity(store_id: int, from_number: str, text: str) -> str:
    try:
        from app.agents.integrity.service import get_service
        return get_service().handle_message(store_id, from_number, text)
    except Exception as e:
        logger.warning("Integrity error store=%d: %s", store_id, e)
        return "Integrity agent unavailable right now. Try again shortly."


def _revenue(store_id: int, from_number: str, text: str) -> str:
    try:
        from app.agents.revenue.registry import get_registry
        reply = get_registry().handle(store_id, from_number, text)
        return reply.text
    except Exception as e:
        logger.warning("Revenue error store=%d: %s", store_id, e)
        return "Revenue advisor unavailable right now. Try again shortly."


def _scout(store_id: int, from_number: str, text: str) -> str:
    try:
        from app.agents.scout.pipeline import run as scout_run
        return scout_run("scout", store_id=store_id)
    except Exception as e:
        logger.warning("Scout error store=%d: %s", store_id, e)
        return "Scout report is being prepared — it'll arrive in a few minutes."


def _reputation(store_id: int, from_number: str, text: str) -> str:
    try:
        from app.agents.reputation import process_reputation_owner_reply
        return process_reputation_owner_reply(from_number, text, store_id=store_id)
    except Exception as e:
        logger.warning("Reputation error store=%d: %s", store_id, e)
        return (
            "Reputation agent unavailable right now.\n"
            "Commands: POST · EDIT <text> · IGNORE · CHECK"
        )
