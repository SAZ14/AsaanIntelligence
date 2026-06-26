"""Internal staff handler — store is already known from the To field.

Keyword routing is transparent to the user: they just type naturally and the
server figures out which agent to call. No agent-selection step needed.

Keywords:
  scout / competitors / intel          → competitor scout
  integrity / audit / leakage / profit
  / staff / void / daily / weekly      → integrity agent
  revenue / sales / pricing / strategy → revenue agent
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
    "  help   — this message\n"
    "  menu   — switch between staff tools and customer app"
)

_SCOUT_TRIGGERS = {"scout", "competitors", "competitor", "intel"}


def handle_internal_for_store(from_number: str, body: str, store_id: int) -> str:
    """Route one staff message to the right internal agent. Returns reply text."""
    text = (body or "").strip()

    if not text or text.lower() == "help":
        return HELP_TEXT

    first = text.lower().split()[0]

    if first in _SCOUT_TRIGGERS:
        return _scout(store_id, from_number, text)

    agent = classify_agent(text)
    if agent == "scout":
        return _scout(store_id, from_number, text)
    if agent == "revenue":
        return _revenue(store_id, from_number, text)

    # Default — integrity handles the widest range of internal queries
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
