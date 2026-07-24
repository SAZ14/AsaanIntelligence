"""Customer-facing handler — store_id already resolved by the unified webhook."""
from __future__ import annotations
import logging
import re

logger = logging.getLogger(__name__)

# The ONLY thing that starts a fresh queue-join: the prefilled text of the
# entrance/booking-area QR code's wa.me link, e.g.
#   https://wa.me/<number>?text=Join%20the%20Queue
# or, for one specific branch of a multi-location store (see
# main.py's get_queue_join_links, which generates exactly this format):
#   https://wa.me/<number>?text=Join%20the%20Queue%20-%20F7
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
    """Alnum-normalised PREFIX match -- tolerant of surrounding emoji/
    punctuation a restaurant might decorate the printed QR text with
    ("🎫 Join the Queue!"), and of a location code appended after the
    phrase for a branch-specific QR ("Join the Queue - F7"). Still
    requires the base phrase to be typed in full at the START of the
    message, not merely present somewhere in it -- "please join the
    queue for me" (ordinary chat) does not qualify, keeping the same
    "nobody accidentally types this" protection the exact-match version
    had. The appended code itself isn't parsed out here: agent.py's
    _book_flow already matches a multi-branch store's location against
    the guest's raw text (match_location in config.py) whenever a branch
    hasn't been picked yet, so whatever follows the base phrase is simply
    left in place for that same matching to find.

    This alone only stops CASUAL/ACCIDENTAL triggering -- it says nothing
    about whether the message actually came from a fresh scan of the
    entrance QR just now, versus someone replaying a remembered or
    screenshotted copy of the same text from home. See
    _extract_entrance_code / _redeem_entrance_code below for the part
    that actually closes that gap."""
    return _alnum(text).startswith(_BOOKING_TRIGGER_NORM)


# The relinker (main.py's entrance_qr_relink, the ONLY thing the printed QR
# encodes) appends " #<code>" to the end of the trigger text on every scan --
# a fresh, single-use, short-TTL code minted server-side, never baked into
# the QR image itself. Requiring it here (see _redeem_entrance_code) is what
# makes a stale screenshot or a remembered "Join the Queue - F7" message
# stop working: the phrase alone is meant to be public and printable, the
# code is meant to die the instant it's used once or its TTL passes.
_ENTRANCE_CODE_RE = re.compile(r"#([A-Za-z0-9]{6,12})\s*$")


def _extract_entrance_code(text: str) -> str | None:
    m = _ENTRANCE_CODE_RE.search(text)
    return m.group(1) if m else None


def _redeem_entrance_code(store_id: int, text: str) -> bool:
    """True only if `text` carries a currently-valid one-time entrance
    code for this store -- burns it (marks used) in the same call, so a
    valid redemption can never be double-spent even by two near-
    simultaneous requests (see Store.redeem_entrance_code's own docstring
    for the atomicity guarantee)."""
    code = _extract_entrance_code(text)
    if not code:
        return False
    from app.agents.maitre_d.store import Store
    ok, _location_id = Store(store_id).redeem_entrance_code(code)
    return ok


def _trigger_location_id(store_id: int, text: str) -> int | None:
    """Which branch (if any) a fresh trigger message names, resolved the
    same way agent.py's own booking flow eventually would (match_location
    against the store's configured branches) -- needed BEFORE the
    entrance code is redeemed, so checking a disabled branch's
    is_booking_enabled doesn't require burning a still-valid code first.
    None for a single-location store (no branch concept) or a message
    that doesn't name one."""
    from app.agents.maitre_d.config import VenueConfig, match_location
    locations = VenueConfig.list_locations(store_id)
    if len(locations) <= 1:
        return None
    matched = match_location(locations, text)
    return matched.location_id if matched else None


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


# ── per-phone rate limit ─────────────────────────────────────────────────────
# Redis-backed (app.core.cache) so it holds across multiple instances and
# restarts, with an in-process fallback for when Redis is unreachable (local
# dev, tests, outage) -- mirrors main.py's _scout_rate_ok exactly, for the
# same reason: failing all the way open during a Redis outage would mean the
# one time this guard matters most (something is actively spamming the
# webhook) is exactly when it silently stops working. Deliberately generous
# (a real back-and-forth conversation can easily hit a dozen turns) -- this
# guards against a script/bot blasting messages, not against a normal chatty
# guest.
import time as _time
from collections import defaultdict as _defaultdict

_CUSTOMER_RATE_WINDOW = 60   # seconds
_CUSTOMER_RATE_MAX = 20
_customer_rate: dict[str, list[float]] = _defaultdict(list)  # phone → [timestamps]


def _customer_rate_ok(phone: str) -> bool:
    """Return True (and record the hit) if this phone is within the rate
    limit for customer-facing messages (both the queue and the community
    agent go through this one gate)."""
    from app.core import cache as _cache
    if _cache.available():
        return _cache.rate_limit_ok(
            f"guard:customer_rate:{phone}", _CUSTOMER_RATE_MAX, _CUSTOMER_RATE_WINDOW
        )
    now = _time.monotonic()
    hits = [t for t in _customer_rate[phone] if now - t < _CUSTOMER_RATE_WINDOW]
    if len(hits) >= _CUSTOMER_RATE_MAX:
        _customer_rate[phone] = hits
        return False
    hits.append(now)
    _customer_rate[phone] = hits
    return True


def handle_customer_for_store(from_phone: str, body: str, store_id: int) -> str:
    """Invoke the customer agent for a known store. Returns reply text.

    Both branches below are gated by this store's package (see
    app.core.entitlements) -- a guest whose store hasn't licensed maitre_d
    or the community agent gets no reply at all rather than a partial or
    confusing one (see main.py's `if reply:` guard around the caller: an
    empty/falsy return here sends nothing back)."""
    try:
        from app.core.entitlements import has_agent_access
        from app.agents.maitre_d.config import normalise_phone

        text = (body or "").strip()
        # Every real webhook (Twilio/OpenWA/Meta) passes from_phone as
        # "whatsapp:+92..." -- normalize it ONCE here, up front, and use
        # that consistently below. Confirmed live as a real bug: this used
        # to pass from_phone through RAW to _has_active_booking_flow/
        # _wants_to_modify_queue_entry, which look up MaitreDConversation/
        # MaitreDQueueEntry rows by an exact phone match -- but those rows
        # are always saved under the NORMALIZED phone (MaitreD.handle_message
        # normalizes internally before touching any storage). The raw vs
        # normalized mismatch meant a guest's SECOND message in a booking
        # flow (e.g. answering "how many people?") could never be recognised
        # as a continuation and silently fell through to the community
        # agent instead -- reproduced live against production, not just in
        # tests (the existing test suite's PHONE constant happened to
        # already be in normalized form, masking this for every prior test).
        phone = normalise_phone(from_phone)

        # Checked before any DB/LLM work -- a spammer's excess messages
        # should cost this process as little as possible. Silent drop, not
        # a "slow down" reply: an automated sender doesn't care, a real
        # human is never going to hit 20 messages/min by accident, and not
        # replying avoids both escalating a spam loop and telegraphing the
        # exact threshold to whoever's testing it.
        if not _customer_rate_ok(phone):
            logger.warning("customer.rate_limit: store=%d phone=%s exceeded", store_id, phone)
            return ""

        # maitre_d (the walk-in queue) is only even considered if this
        # store's package includes it -- otherwise none of the booking-
        # related triggers below (including "Join the Queue" itself) are
        # allowed to start or continue a flow, exactly as if the queue
        # feature didn't exist for this store at all.
        if has_agent_access(store_id, "maitre_d"):
            from app.agents.maitre_d.config import is_booking_enabled

            active_flow = _has_active_booking_flow(store_id, phone)
            wants_cancel = _is_cancel_message(text)
            wants_modify = _wants_to_modify_queue_entry(store_id, phone, text)
            # A fresh trigger only starts a flow when booking is actually
            # switched on for the BRANCH it names -- staff's "disable
            # booking" for a quiet walk-in day (see internal.py), which is
            # now per-branch for a multi-location store. The branch is
            # resolved from the message text itself, the same way agent.py's
            # own booking flow eventually would, and checked BEFORE the
            # entrance code is touched -- a scan of a branch that's
            # currently closed must not burn an otherwise-still-valid code
            # for nothing. Deliberately checked ONLY for the fresh-start
            # case: an already-active flow finishes even if staff flip the
            # toggle mid-conversation (less confusing than abandoning a guest
            # partway through), and "cancel"/"modify" always work regardless
            # (neither can create a queue entry, see their own docstrings).
            #
            # A fresh trigger ALSO now needs a currently-valid one-time
            # entrance code (see _redeem_entrance_code) -- this is the part
            # that actually verifies "this came from a scan just now", not
            # just "this text matches the trigger phrase". An active flow or
            # cancel/modify never needs one: neither can create a fresh
            # queue entry, so neither carries the replay risk a fresh join
            # does.
            invalid_code = False
            wants_new_booking = False
            if _is_booking_trigger(text) and not active_flow:
                trigger_location_id = _trigger_location_id(store_id, text)
                if is_booking_enabled(store_id, trigger_location_id):
                    if _redeem_entrance_code(store_id, text):
                        wants_new_booking = True
                    else:
                        invalid_code = True
                # else: booking is off for this specific branch (or the
                # whole store) -- falls through silently below, same as
                # the store-wide case always has; the code (if any) is
                # never touched, so it stays valid for a later scan once
                # booking's back on.

            if active_flow or wants_cancel or wants_modify or wants_new_booking:
                return _handle_booking(phone, text, store_id)

            if invalid_code:
                return ("That entrance code has expired or was already used. "
                        "Please scan the QR code at the entrance for a fresh one.")

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
        # handle_customer_message re-parses phone itself (parse_twilio_
        # whatsapp_phone strips "whatsapp:" if present either way), so
        # passing the already-normalized phone here is safe either way.
        reply = handle_customer_message(phone, body, store_id=store_id)
        return reply.body
    except Exception as e:
        logger.error("Customer agent error store=%d: %s", store_id, e)
        return "Something went wrong, please try again."
