"""Customer-facing handler — store_id already resolved by the unified webhook."""
from __future__ import annotations
import logging
import re

logger = logging.getLogger(__name__)

# The ONLY thing that starts a fresh queue-join: the exact prefilled text
# of the entrance/booking-area QR code's wa.me link, e.g.
#   https://wa.me/<number>?text=Join%20the%20Queue
# Deliberately NOT a keyword match on "book"/"table"/"reservation" (that
# used to be the trigger) -- a table's own QR code can be scanned by
# someone already seated there, and a wa.me link's prefilled text is just
# a suggestion the sender can edit or ignore, so nothing in the message
# itself can prove "this came from the entrance QR". Restricting the
# trigger to one specific, non-conversational phrase is the practical
# mitigation: nobody accidentally types "Join the Queue" while chatting,
# unlike the single common word "book" ("I need to book a table sometime"
# used to accidentally start a queue-join). It's not cryptographically
# unforgeable -- nothing over plain WhatsApp can be -- but it closes the
# casual/accidental case, which is the actual threat model here (a guest
# already seated by staff, no prior interaction with the bot at all,
# shouldn't be able to just type their way into the queue).
BOOKING_TRIGGER_PHRASE = "Join the Queue"


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


_BOOKING_TRIGGER_NORM = _alnum(BOOKING_TRIGGER_PHRASE)


def _is_booking_trigger(text: str) -> bool:
    """Alnum-normalised exact match -- tolerant of surrounding emoji/
    punctuation a restaurant might decorate the printed QR text with
    ("🎫 Join the Queue!"), but still a specific phrase, not a loose
    keyword-in-free-text match."""
    return _alnum(text) == _BOOKING_TRIGGER_NORM


def _is_cancel_message(text: str) -> bool:
    """"cancel" is always allowed, trigger-phrase or not -- it only ever
    removes an EXISTING queue entry (see agent.py's _leave_queue), never
    creates one, so it carries none of the "typed their way into the
    queue" risk the booking trigger above guards against."""
    return bool(re.search(r"\bcancel\b", text.lower()))


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
        from app.agents.maitre_d.config import is_booking_enabled

        text = (body or "").strip()
        active_flow = _has_active_booking_flow(store_id, from_phone)
        wants_cancel = _is_cancel_message(text)
        # A fresh trigger only starts a flow when booking is actually
        # switched on -- staff's "disable booking" for a quiet walk-in day
        # (see internal.py). Deliberately checked ONLY for the fresh-start
        # case: an already-active flow finishes even if staff flip the
        # toggle mid-conversation (less confusing than abandoning a guest
        # partway through), and "cancel" always works regardless (it only
        # removes, see _is_cancel_message).
        wants_new_booking = _is_booking_trigger(text) and is_booking_enabled(store_id)

        if active_flow or wants_cancel or wants_new_booking:
            return _handle_booking(from_phone, text, store_id)

        # Not a recognised booking trigger (including a disabled "Join the
        # Queue", or plain text like "book"/"table for 2" typed by someone
        # who never scanned the entrance QR) -- silently falls through to
        # the normal community agent below, exactly like any other message
        # maitre_d doesn't handle. No "booking is off" reply; the guest
        # never needs to know the queue exists at all if they didn't scan
        # the right QR.
        from app.agents.customer.agents.community_customer import handle_customer_message
        reply = handle_customer_message(from_phone, body, store_id=store_id)
        return reply.body
    except Exception as e:
        logger.error("Customer agent error store=%d: %s", store_id, e)
        return "Something went wrong, please try again."
