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


_MODIFY_TRIGGER_RE = re.compile(r"\b(?:party of\s*\d|change|update|actually|make it)\b", re.IGNORECASE)


def _llm_confirms_modify_intent(text: str) -> bool:
    """Final gatekeeper before treating a message as a queue-entry update
    -- the regex above is only a cheap pre-filter (its trigger words are
    ordinary English: "change"/"update"/"actually"/"make it" all show up
    in completely unrelated chatter too, e.g. a guest already in the
    queue asking "actually can I get extra napkins"). Uses the same
    shared ZAI client every other agent's routing/classification calls
    go through (see app.core.llm.get_fast_model's own docstring -- it's
    built for exactly this: "message routing... intent classification").

    Fails CLOSED: any error or timeout is treated as NOT a modify request,
    falling through to the community agent. A missed modify attempt just
    means the guest rephrases or says "cancel" and rejoins -- much
    cheaper than silently misapplying a change to their queue entry
    based on a misread message."""
    try:
        from app.core.llm import get_client, get_fast_model, nothink_kwargs
        client = get_client()
        model = get_fast_model()
        resp = client.chat.completions.create(
            model=model,
            max_tokens=5,
            temperature=0,
            # Tight timeout, same reasoning as nlu.py's own LLM call: the
            # underlying client's max_retries=1 makes this a ceiling PER
            # ATTEMPT, and the fail-closed fallback above exists precisely
            # so a slow/failed call never blocks or misroutes a reply.
            timeout=6.0,
            messages=[{
                "role": "user",
                "content": (
                    "A restaurant customer who is CURRENTLY WAITING in a "
                    "walk-in queue sent this message. Is it a request to "
                    "change their party size or the name on their queue "
                    "entry (e.g. \"actually we're 5 now\", \"change it to "
                    "4 people\", \"put it under Bilal instead\")? Reply "
                    "with exactly one word, YES or NO -- nothing else.\n\n"
                    f"Message: \"{text}\""
                ),
            }],
            **nothink_kwargs(model),
        )
        answer = resp.choices[0].message.content.strip().upper()
        return answer.startswith("YES")
    except Exception:
        return False


def _wants_to_modify_queue_entry(store_id: int, phone: str, text: str) -> bool:
    """A guest already in the queue changing their party size or name --
    e.g. "actually we're 5 now", "change it to 5 people". Layered so the
    (rare, one-call) LLM classification only ever runs for someone who's
    both said something modify-shaped AND is genuinely in the queue --
    unrelated chatter, or anyone with no active entry, never reaches it
    at all, let alone the community agent's normal message volume."""
    if not _MODIFY_TRIGGER_RE.search(text):
        return False
    from app.agents.maitre_d.store import Store
    if Store(store_id).latest_waiting_entry_for(phone) is None:
        return False
    return _llm_confirms_modify_intent(text)


def _has_active_booking_flow(store_id: int, phone: str) -> bool:
    """True if this guest is mid-way through a Maitre D slot-filling
    conversation (e.g. just asked "how many people?" and hasn't answered
    yet) -- those follow-ups ("4", "Ahmed") won't match any of the
    exemptions above, so a guest wouldn't otherwise fall back into the
    right agent.

    Passes the VENUE-local clock explicitly -- get_conversation()'s own
    default (server time) is wrong here: this server's clock reads
    hours behind Asia/Karachi, so the TTL comparison would always come
    out negative (never "expired"). Confirmed as a real bug: a guest who
    starts but never finishes a flow would stay "active" forever, which
    would let them later type anything at all and have it keep routing
    back into booking -- quietly defeating the whole point of restricting
    fresh joins to the QR trigger phrase."""
    from app.agents.maitre_d.store import Store
    from app.agents.maitre_d.config import VenueConfig

    store = Store(store_id)
    cfg = VenueConfig.load(store_id)
    return bool(store.get_conversation(
        phone, ttl_minutes=cfg.conversation_ttl_minutes, now=cfg.now(),
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
    """Invoke the customer agent for a known store. Returns reply text.

    Both branches below are gated by this store's package (see
    app.core.entitlements) -- a guest whose store hasn't licensed maitre_d
    or the community agent gets no reply at all rather than a partial or
    confusing one (see main.py's `if reply:` guard around the caller: an
    empty/falsy return here sends nothing back)."""
    try:
        from app.core.entitlements import has_agent_access

        text = (body or "").strip()

        # maitre_d (the walk-in queue) is only even considered if this
        # store's package includes it -- otherwise none of the booking-
        # related triggers below (including "Join the Queue" itself) are
        # allowed to start or continue a flow, exactly as if the queue
        # feature didn't exist for this store at all.
        if has_agent_access(store_id, "maitre_d"):
            from app.agents.maitre_d.config import is_booking_enabled

            active_flow = _has_active_booking_flow(store_id, from_phone)
            wants_cancel = _is_cancel_message(text)
            wants_modify = _wants_to_modify_queue_entry(store_id, from_phone, text)
            # A fresh trigger only starts a flow when booking is actually
            # switched on -- staff's "disable booking" for a quiet walk-in day
            # (see internal.py). Deliberately checked ONLY for the fresh-start
            # case: an already-active flow finishes even if staff flip the
            # toggle mid-conversation (less confusing than abandoning a guest
            # partway through), and "cancel"/"modify" always work regardless
            # (neither can create a queue entry, see their own docstrings).
            wants_new_booking = _is_booking_trigger(text) and is_booking_enabled(store_id)

            if active_flow or wants_cancel or wants_modify or wants_new_booking:
                return _handle_booking(from_phone, text, store_id)

        # Not a recognised booking trigger (including a disabled "Join the
        # Queue", or plain text like "book"/"table for 2" typed by someone
        # who never scanned the entrance QR) -- silently falls through to
        # the normal community agent below, exactly like any other message
        # maitre_d doesn't handle. No "booking is off" reply; the guest
        # never needs to know the queue exists at all if they didn't scan
        # the right QR.
        if not has_agent_access(store_id, "customer"):
            return ""
        from app.agents.customer.agents.community_customer import handle_customer_message
        reply = handle_customer_message(from_phone, body, store_id=store_id)
        return reply.body
    except Exception as e:
        logger.error("Customer agent error store=%d: %s", store_id, e)
        return "Something went wrong, please try again."
