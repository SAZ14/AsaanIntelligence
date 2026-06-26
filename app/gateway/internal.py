"""Internal staff WhatsApp handler.

One shared Twilio number for all internal agents (scout, integrity, revenue).
Only numbers in store_members are allowed. Routing is keyword-first with
graceful fallback.

Commands:
  scout / competitors / intel   → competitor scout report (queued)
  integrity / audit / leakage / profit / staff / summary / ... → integrity agent
  revenue / sales / pricing / strategy / ...                   → revenue agent
  switch / change                                               → pick a different store
  help                                                          → command list
"""
from __future__ import annotations
import logging
from xml.sax.saxutils import escape

from app.core.db import get_stores_for_number, get_user_session, set_user_session
from app.core.routing import classify_agent, is_switch_intent, parse_store_selection, store_menu

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "AsaanPay Internal Channel\n\n"
    "Integrity:\n"
    "  audit / summary / leakage / profit / staff / daily / weekly / refresh\n\n"
    "Revenue:\n"
    "  revenue / sales / pricing / strategy / upsell / campaigns / menu\n\n"
    "Scout (weekly auto-report):\n"
    "  scout → triggers competitor report now\n\n"
    "  switch → change restaurant\n"
    "  help   → this message"
)

_SCOUT_TRIGGERS = {"scout", "competitors", "competitor", "intel"}


def handle_internal_message(from_number: str, body: str) -> str:
    """Route an internal staff message. Returns the reply text."""
    text = (body or "").strip()
    stores = get_stores_for_number(from_number)

    if not stores:
        return "Your number isn't registered. Ask your admin to add you via /admin/stores/{id}/members."

    # Switch intent
    if is_switch_intent(text):
        if len(stores) == 1:
            return f"You only have one restaurant: {stores[0].name}."
        set_user_session(from_number, None)
        return store_menu(stores)

    session = get_user_session(from_number)

    # Awaiting store selection
    if session is not None and session.store_id is None:
        selected = parse_store_selection(text, stores)
        if selected:
            set_user_session(from_number, selected.id)
            store_id = selected.id
        else:
            return store_menu(stores)
    elif len(stores) == 1:
        store_id = stores[0].id
    elif session and session.store_id:
        store_id = session.store_id
    else:
        set_user_session(from_number, None)
        return store_menu(stores)

    if not text or text.lower() in ("help", "start", "hi", "hello", "menu"):
        return HELP_TEXT

    first_word = text.lower().split()[0]

    # Scout
    if first_word in _SCOUT_TRIGGERS:
        return _handle_scout(store_id, from_number, text)

    # Classify and route
    agent = classify_agent(text)
    if agent == "scout":
        return _handle_scout(store_id, from_number, text)
    if agent == "integrity":
        return _handle_integrity(store_id, from_number, text)
    if agent == "revenue":
        return _handle_revenue(store_id, from_number, text)

    # Default to integrity (most common for internal use)
    return _handle_integrity(store_id, from_number, text)


def _handle_integrity(store_id: int, from_number: str, text: str) -> str:
    try:
        from app.agents.integrity.service import get_service
        return get_service().handle_message(store_id, from_number, text)
    except Exception as e:
        logger.warning("Integrity agent error store=%d: %s", store_id, e)
        return "Integrity agent unavailable right now. Try again shortly."


def _handle_revenue(store_id: int, from_number: str, text: str) -> str:
    try:
        from app.agents.revenue.registry import get_registry
        reply = get_registry().handle(store_id, from_number, text)
        return reply.text
    except Exception as e:
        logger.warning("Revenue agent error store=%d: %s", store_id, e)
        return "Revenue advisor unavailable right now. Try again shortly."


def _handle_scout(store_id: int, from_number: str, text: str) -> str:
    try:
        from app.agents.scout.pipeline import run as scout_run
        report = scout_run("scout", store_id=store_id)
        return report
    except Exception as e:
        logger.warning("Scout agent error store=%d: %s", store_id, e)
        return "Scout report is being prepared — it'll arrive in a few minutes."
