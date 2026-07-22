"""Customer-facing handler — store_id already resolved by the unified webhook."""
from __future__ import annotations
import logging
import re

logger = logging.getLogger(__name__)


def _wants_booking(text: str) -> bool:
    """Keyword pre-check for routing to the Maitre D, mirroring gateway/
    main.py's _is_scout_message pattern: cheap and deterministic, just
    enough to decide WHICH agent handles this turn. Once routed there,
    Maitre D's own LLM/fallback NLU (app.agents.maitre_d.nlu) does the
    real understanding. Shares _MAITRE_D_KEYWORDS with internal.py's
    staff-side routing -- see main.py's definition."""
    from app.gateway.main import _MAITRE_D_KEYWORDS
    words = set(re.sub(r"[^\w\s]", "", text.lower()).split())
    return bool(words & _MAITRE_D_KEYWORDS)


def _has_active_booking_flow(store_id: int, phone: str) -> bool:
    """True if this guest is mid-way through a Maitre D slot-filling
    conversation (e.g. just asked "what time works?" and hasn't answered
    yet) -- those follow-ups ("8pm", "Ahmed", "yes") won't contain a
    booking keyword, so a guest wouldn't otherwise fall back into the
    right agent."""
    from app.agents.maitre_d.store import Store
    from app.agents.maitre_d.config import VenueConfig

    store = Store(store_id)
    cfg = VenueConfig.load(store_id)
    return bool(store.get_conversation(
        phone, ttl_minutes=cfg.conversation_ttl_minutes,
    ).get("flow"))


def _handle_booking(from_phone: str, body: str, store_id: int) -> str:
    from app.agents.maitre_d.agent import get_maitre_d
    from app.core.outbound import send_from_store, notify_staff

    md = get_maitre_d(store_id)
    reply = md.handle_message(from_phone, body)

    # Proactive messages to OTHER guests, if the agent ever needs to send
    # one -- this turn's own reply still goes back via the caller's normal
    # send_fn, only side-effect sends happen here.
    for phone, text in reply.outbound:
        send_from_store(store_id, phone, text)

    if reply.staff_alert:
        notify_staff(store_id, f"[Reservations] {reply.staff_alert}")

    return reply.text


def handle_customer_for_store(from_phone: str, body: str, store_id: int) -> str:
    """Invoke the customer agent for a known store. Returns reply text."""
    try:
        text = (body or "").strip()
        if _wants_booking(text) or _has_active_booking_flow(store_id, from_phone):
            return _handle_booking(from_phone, text, store_id)

        from app.agents.customer.agents.community_customer import handle_customer_message
        reply = handle_customer_message(from_phone, body, store_id=store_id)
        return reply.body
    except Exception as e:
        logger.error("Customer agent error store=%d: %s", store_id, e)
        return "Something went wrong, please try again."
