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
import re as _re

logger = logging.getLogger(__name__)

_GREETINGS = {
    "hi", "hello", "hey", "hy", "hii", "start", "commands",
    "salam", "assalam", "asalam", "aoa", "assalamualaikum", "asalamualaikum",
}


def staff_help_text(store_name: str) -> str:
    """The one canonical staff-tools message -- shown after selecting mode 1,
    on a bare "help", and on any greeting. Previously main.py's mode-select
    welcome and this module's HELP_TEXT were two separate, drifting copies
    (the welcome was missing "refresh"); a greeting could also fall through
    the LLM classifier into one specific agent (usually integrity, whose own
    handle_message() had its own "hi"/"hello" special case returning ONLY
    its own commands) instead of ever reaching either of them. One function,
    always the full command list across every agent (integrity, revenue,
    scout, reputation, and reservations/maitre_d)."""
    return (
        f"Staff tools: {store_name}\n\n"
        "*Integrity*: POS audit & leakage\n"
        "  summary: full overview\n"
        "  leakage: theft & voids breakdown\n"
        "  profit: margins & COGS\n"
        "  staff: per-staff anomalies\n"
        "  daily / weekly: period report\n"
        "  refresh: re-sync latest POS data\n\n"
        "*Revenue*: Sales & strategy\n"
        "  revenue: overall sales performance\n"
        "  sales: item & category breakdown\n"
        "  pricing: price optimisation tips\n"
        "  strategy: growth recommendations\n\n"
        "*Scout*: Competitor intelligence\n"
        "  scout: scrape rivals (cached 24h)\n\n"
        "*Reputation*: Review management\n"
        "  check: scrape latest reviews (cached 24h)\n"
        "  positive reviews / negative reviews / all reviews: list reviews, 10 at a time\n"
        "  next: see the next 10\n"
        "  post: mark suggested reply as replied (post it yourself first)\n"
        "  ignore: skip current review\n"
        "  edit <text>: rewrite suggested reply\n\n"
        "*Reservations*: The book & the door\n"
        "  reservations: upcoming bookings (all branches)\n"
        "  waitlist: who's waiting for a table\n"
        "  vip list: your VIP guests\n"
        "  branches: your locations and what they take\n"
        "  add vip <phone> <name>[, notes]: add a VIP\n"
        "  seat/complete/noshow <id>: update a booking (id from *reservations*)\n\n"
        "You can also just write in plain language, e.g. \"how did we do this "
        "week\" or \"what are competitors offering\", no need to remember exact "
        "commands.\n\n"
        "Type *menu* to switch modes, or *reset* to clear the conversation and start fresh."
    )


def _get_store_name(store_id: int) -> str:
    from app.core.db import SessionLocal, Store
    with SessionLocal() as db:
        store = db.query(Store).filter(Store.id == store_id).first()
        return store.name if store else "your restaurant"


# ── Staff conversational memory ─────────────────────────────────────────────
# Reuses the customer agent's chat-session storage (Redis, 1h TTL + Postgres
# durability -- app/agents/customer/community/store.py) via a "staff:"-
# prefixed phone key, rather than duplicating that load/save/cache pattern.
# The prefix matters: the same phone number can be in customer mode or staff
# mode at different times (mode switching is a real, supported flow), so a
# shared bare-phone key would bleed customer-facing chit-chat into staff
# tool answers and vice versa. Capped at the last 6 messages (3 turns), same
# window customer mode already uses -- enough for real follow-ups ("what
# about last week") without letting a long-stale conversation drift the
# router or an agent's answer off onto an unrelated earlier topic.
_STAFF_HISTORY_TURNS = 6


def _load_staff_history(store_id: int, phone: str) -> list[dict]:
    from app.agents.customer.community.store import load_chat_session
    return load_chat_session(store_id, f"staff:{phone}")


def _save_staff_turn(store_id: int, phone: str, history: list[dict], user_text: str, reply: str) -> None:
    from app.agents.customer.community.store import save_chat_session
    updated = list(history) + [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": reply},
    ]
    save_chat_session(store_id, f"staff:{phone}", updated[-_STAFF_HISTORY_TURNS:])


def _condensed_history(history: list[dict], max_assistant_len: int = 150) -> list[dict]:
    """Trims prior assistant replies for the router's classification call --
    it only needs enough to resolve a reference ("what about last week"),
    not the full text of a 2000-character report, which would just add
    latency/cost to what's meant to be a sub-second classification."""
    condensed = []
    for turn in history:
        content = turn.get("content", "")
        if turn.get("role") == "assistant" and len(content) > max_assistant_len:
            content = content[:max_assistant_len] + "..."
        condensed.append({"role": turn.get("role", "user"), "content": content})
    return condensed


# ── Router continuity safety net ────────────────────────────────────────────
# Confirmed live: even at temperature=0, the LLM router occasionally
# misroutes a short, keyword-free continuation message ("draft a reply I
# can send them" mid-way through a reviews conversation) to a completely
# unrelated agent -- reproduced twice out of several attempts. Not a new
# problem (the router was never perfectly deterministic), but jarring now
# that memory makes conversations otherwise feel coherent: getting a random
# revenue pitch mid-review-discussion breaks the illusion harder than a
# context-free bot ever could. This is a zero-latency, deterministic
# correction layered under the LLM call rather than a second LLM call
# (self-consistency/voting would fix this too, but at 2-3x the router's
# cost and latency for a rare edge case) -- reuses the SAME keyword sets
# gateway/main.py already has for the ack-text guess, so "does this
# message contain a real signal for switching topics" is answered the
# identical way everywhere in this codebase.

_LAST_AGENT_TTL = 7200  # matches staff chat history's TTL


def _topic_signal(text: str) -> str | None:
    """Which agent (if any) this text contains a strong keyword signal
    for, independent of the LLM's classification. None means the message
    doesn't clearly point anywhere -- exactly the case where sticking
    with the previous turn's agent is safer than trusting a possibly-
    flaky classification."""
    from app.gateway.main import (
        _is_scout_message, _INTEGRITY_SHORTHAND, _REVIEW_KEYWORDS,
        _REVENUE_KEYWORDS, _MAITRE_D_KEYWORDS,
    )

    if _is_scout_message(text):
        return "scout"
    words = set(_re.sub(r"[^\w\s]", "", text.lower()).split())
    if words & _MAITRE_D_KEYWORDS:
        return "maitre_d"
    if words & _REVIEW_KEYWORDS:
        return "reputation"
    if words & _REVENUE_KEYWORDS:
        return "revenue"
    if words & _INTEGRITY_SHORTHAND:
        return "integrity"
    return None


def _load_last_agent(store_id: int, phone: str) -> str | None:
    from app.core import cache as _cache
    return _cache.get(f"last_agent:{store_id}:{phone}")


def _save_last_agent(store_id: int, phone: str, agent: str) -> None:
    from app.core import cache as _cache
    _cache.set(f"last_agent:{store_id}:{phone}", agent, ttl=_LAST_AGENT_TTL)


_RESET_TRIGGERS = {"reset", "new chat", "clear", "clear chat", "start over", "forget"}


def _clear_staff_memory(store_id: int, phone: str) -> None:
    """Wipes this staff member's conversational history and router
    continuity (_load_staff_history / _load_last_agent) so a stale or
    derailed conversation stops influencing later turns. Self-service via
    typing one of _RESET_TRIGGERS -- there was previously no way to do
    this short of waiting out the 2h Redis TTL."""
    from app.agents.customer.community.store import save_chat_session
    from app.core import cache as _cache
    save_chat_session(store_id, f"staff:{phone}", [])
    _cache.delete(f"last_agent:{store_id}:{phone}")


_REPUTATION_EXACT = {"post", "ignore", "next"}

# Documented Revenue Advisor shorthand commands (see staff_help_text). These bypass
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


def _classify_with_llm(text: str, history: list[dict] | None = None) -> tuple[str, str]:
    """Return (agent, command) via ZAI. Falls back to keyword routing."""
    try:
        from app.core.llm import get_client, get_fast_model, nothink_kwargs
        client = get_client()
        fast_model = get_fast_model()

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
            "integrity/free_form – any other AUDIT/RECONCILIATION question not covered above (payment mismatches, tax anomalies) — NOT product sales performance or timing, those are revenue/general\n"
            "revenue/general     – growth strategy, upsell tips, campaigns, how to sell more, business advice, best/top sellers, what's selling, slow/quiet/dead times, pricing changes, menu mix, repeat customers/loyalty\n"
            "reputation/check    – SCRAPE new reviews right now: 'check reviews', 'get latest reviews'\n"
            "reputation/positive – SHOW/LIST positive reviews specifically: 'show good reviews', 'what are people happy about'\n"
            "reputation/negative – SHOW/LIST negative/bad reviews specifically: 'show bad reviews', 'what are people complaining about'\n"
            "reputation/reviews  – SHOW/LIST reviews with no sentiment filter: 'show all reviews', 'list reviews'\n"
            "reputation/chat     – other questions ABOUT reviews that aren't a show/list request: trends, ratings over time, general \"how are we doing on reviews\"\n"
            "scout/scout         – competitor intelligence, rival restaurants, what competitors are doing\n"
            "maitre_d/reservations – table reservations, waitlist, no-shows, VIP guests, the door (seat/complete/no-show a booking) — NOT loyalty stamps, NOT the customer's own booking (that's the guest-facing flow, this is staff asking ABOUT bookings)\n\n"
            "CONVERSATION CONTINUITY: if earlier turns are shown above, a short "
            "follow-up that names no clear new topic ('draft a reply for them', "
            "'what about that one', 'send it', 'why', 'when was that') is almost "
            "always CONTINUING the SAME topic as the most recent turn, not "
            "switching to a different agent. Only switch agents when the message "
            "itself names a genuinely different, unrelated subject.\n\n"
            "DISAMBIGUATION RULES (apply these when in doubt):\n"
            "• 'how much did we make/sell today/this week' (a TOTAL/aggregate figure) → integrity/daily or integrity/weekly — POS data query, NOT strategy\n"
            "• 'best sellers' / 'top products' / 'what's selling' / 'how is X doing vs Y' (comparing SALES VOLUME/units/revenue of specific items) → revenue/general — product performance, NOT integrity\n"
            "• 'margin per item' / 'profit per item' / 'COGS per item' / 'which items are profitable' (comparing COST/MARGIN, not sales volume) → integrity/profit, NOT revenue -- 'margin' or 'profit' or 'COGS' always wins over 'per item' phrasing\n"
            "• 'when are we slow' / 'quiet times' / 'dead hours' / 'off-peak' → revenue/general — dead-window analysis, NOT integrity\n"
            "• 'how to increase sales' / 'grow revenue' / 'new campaign' / 'marketing' → revenue/general\n"
            "• 'check reviews' / 'get new reviews' / 'review lao' / 'koi naye reviews' → reputation/check\n"
            "• asking to SEE/LIST/SHOW reviews of a specific sentiment ('good reviews', 'bad reviews', 'positive feedback', 'complaints', 'acha kya bola', 'bura kya bola') → reputation/positive or reputation/negative, NOT reputation/chat\n"
            "• asking to SEE/LIST/SHOW reviews with no sentiment specified ('show reviews', 'list reviews') → reputation/reviews\n"
            "• a general question ABOUT reviews that isn't asking to see a list ('how is our rating trending', 'log kya bol rahe hain', 'are people happy overall') → reputation/chat\n"
            "• asking what's already been POSTED/REPLIED TO/IGNORED/SKIPPED among reviews → reputation/chat (review workflow status, NOT integrity or revenue even though the words 'posted'/'skip' sound generic)\n"
            "• asking about a SPECIFIC named facility/amenity topic (wifi, parking, noise, cleanliness, seating, ambiance, wait times) → reputation/chat even though the topic itself sounds like operations, e.g. 'any issues with wifi', 'has anyone complained about parking' -- this is asking what's IN THE REVIEWS about that one thing, NOT revenue or integrity, and NOT reputation/negative either since it's not asking for the full negative list\n"
            "• a GENERIC complaint question with no specific topic named ('what did customers complain about', 'shikayat kya ki', 'kya complaints hain') → reputation/negative, this IS a request to see the negative reviews\n"
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
            "  'show me positive reviews' → reputation/positive\n"
            "  'what are the bad reviews' → reputation/negative\n"
            "  'acha kya bola logon ne' → reputation/positive\n"
            "  'show me all reviews' → reputation/reviews\n"
            "  'log kya bol rahe hain' → reputation/chat\n"
            "  'how is our rating trending' → reputation/chat\n"
            "  'what have we already replied to' → reputation/chat\n"
            "  'what did we skip' → reputation/chat\n"
            "  'any issues with wifi or internet?' → reputation/chat\n"
            "  'has anyone complained about parking' → reputation/chat\n"
            "  'how do we upsell desserts' → revenue/general\n"
            "  'what are our best sellers' → revenue/general\n"
            "  'when are we slow during the week' → revenue/general\n"
            "  'how are fries doing compared to burgers' → revenue/general\n"
            "  'what are competitors offering' → scout/scout\n"
            "  'profit margin kya hai' → integrity/profit\n"
            "  'give me a full summary' → integrity/summary\n"
            "  'data refresh karo' → integrity/refresh\n"
            "  'can you check our google reviews' → reputation/check\n"
            "  'increase karni hai sales' → revenue/general\n"
            "  'who's booked in tonight' → maitre_d/reservations\n"
            "  'any VIPs coming this week' → maitre_d/reservations\n"
            "  'seat the 8pm table for Ahmed' → maitre_d/reservations\n"
            "  'who's on the waitlist' → maitre_d/reservations\n\n"
            "  Given prior turns discussing a specific negative review:\n"
            "  'draft a reply I can send them' → reputation/chat -- a bare "
            "'draft a reply' names no topic on its own; it continues whatever "
            "the conversation was already about\n"
            "  'when was that posted' → reputation/chat -- same continuation logic\n"
        )
        # Routing is a 25-token classification — the fast non-reasoning model
        # answers in <1s vs ~10s of thinking on glm-4.7 (verified 10/10 on an
        # English/Urdu routing eval before switching). Tight timeout: if the
        # API stalls, the keyword fallback below routes instead of making
        # staff wait out an API hiccup.
        messages = [{"role": "system", "content": system}]
        messages.extend(_condensed_history(history or []))
        messages.append({"role": "user", "content": text})
        resp = client.chat.completions.create(
            model=fast_model,
            messages=messages,
            temperature=0,
            max_tokens=25,
            timeout=8.0,
            **nothink_kwargs(fast_model),
        )
        result = resp.choices[0].message.content.strip().lower()
        if "/" in result:
            agent, command = result.split("/", 1)
            agent = agent.strip()
            command = command.strip()
            if agent in ("integrity", "revenue", "reputation", "scout", "maitre_d"):
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
        from app.core.llm import get_client, get_fast_model, nothink_kwargs
        from app.core.persona import WHATSAPP_FORMAT_RULES
        try:
            client = get_client()
        except RuntimeError:
            return raw_response
        # Pure rewrite task — no reasoning needed, and the fast model keeps
        # numbers intact just as reliably (1.5s vs 17.7s measured live).
        # Timeout: the raw agent reply is already a complete answer, so a
        # stalled rewrite is never worth waiting for.
        fast_model = get_fast_model()
        resp = client.chat.completions.create(
            timeout=12.0,
            model=fast_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "A restaurant manager asked a question on WhatsApp. "
                        "You have the system output. "
                        "Rewrite it as a direct, conversational answer to their specific question. "
                        "Keep all numbers and data intact. Never invent information not in the "
                        f"system output.\n\n{WHATSAPP_FORMAT_RULES}"
                    ),
                },
                {
                    "role": "user",
                    "content": f"Manager asked: {original_query}\n\nSystem output:\n{raw_response}",
                },
            ],
            temperature=0.3,
            max_tokens=700,
            **nothink_kwargs(fast_model),
        )
        adapted = resp.choices[0].message.content.strip()
        logger.info("internal.adapt_response: adapted %d chars -> %d chars", len(raw_response), len(adapted))
        return adapted
    except Exception as exc:
        logger.warning("internal.adapt_response: failed (%s) — returning raw", exc)
        return raw_response


def handle_internal_for_store(from_number: str, body: str, store_id: int) -> str:
    """Route one staff message to the right agent. Returns reply text.

    Loads/saves short-term conversational memory (_load_staff_history /
    _save_staff_turn) around every path except the static help/greeting
    short-circuit -- even a shorthand command's reply becomes useful
    context for a later natural-language follow-up ("why is that leakage
    number so high" right after "leakage"), so every turn is saved
    regardless of which branch answered it. A single exit point (the
    `reply = ...; break` pattern below) is what makes saving-once-at-the-
    end possible without wrapping every branch in its own save call."""
    text = (body or "").strip()

    if text.lower() in _RESET_TRIGGERS:
        _clear_staff_memory(store_id, from_number)
        logger.info("internal.routing: memory_reset store=%d from=%s", store_id, from_number)
        return "Cleared, starting fresh. What do you need?"

    if not text or text.lower() in ("help", *_GREETINGS):
        return staff_help_text(_get_store_name(store_id))

    first = text.lower().split()[0]
    is_natural = not _is_shorthand(text)
    history = _load_staff_history(store_id, from_number)

    # Cheap pre-check before touching the DB: only "seat"/"complete"/
    # "noshow"/"no show" can possibly be a door command.
    door_reply = None
    if first in ("seat", "complete", "noshow", "no"):
        door_reply = _maitre_d_door_shorthand(store_id, first, text)

    # Unambiguous action commands — skip LLM
    if first in _REPUTATION_EXACT:
        agent = "reputation"
        logger.info("internal.routing: store=%d agent=reputation trigger=action from=%s", store_id, from_number)
        reply = _reputation(store_id, from_number, text, history=history)

    elif first == "edit" and len(text.split()) > 1:
        agent = "reputation"
        logger.info("internal.routing: store=%d agent=reputation trigger=edit from=%s", store_id, from_number)
        reply = _reputation(store_id, from_number, text, history=history)

    elif first in _REVENUE_EXACT and len(text.split()) == 1:
        agent = "revenue"
        logger.info("internal.routing: store=%d agent=revenue trigger=exact from=%s", store_id, from_number)
        reply = _revenue(store_id, from_number, text)

    elif door_reply is not None:
        agent = "maitre_d"
        logger.info("internal.routing: store=%d agent=maitre_d trigger=door from=%s", store_id, from_number)
        reply = door_reply

    elif text.lower().strip() in ("waitlist", "vip", "vips", "vip list", "reservations", "bookings", "locations", "branches"):
        agent = "maitre_d"
        logger.info("internal.routing: store=%d agent=maitre_d trigger=listing from=%s", store_id, from_number)
        reply = _maitre_d_listing(store_id, text.lower().strip())

    elif first == "add" and len(text.split()) > 1 and text.split()[1].lower() == "vip":
        agent = "maitre_d"
        logger.info("internal.routing: store=%d agent=maitre_d trigger=add_vip from=%s", store_id, from_number)
        from app.agents.maitre_d.staff import add_vip
        reply = add_vip(store_id, text.split(None, 2)[2] if len(text.split(None, 2)) > 2 else "")

    else:
        # LLM classification
        agent, command = _classify_with_llm(text, history=history)

        # Continuity safety net: a short, keyword-free continuation
        # ("draft a reply I can send them") has been confirmed live to
        # occasionally flip to a completely unrelated agent even with
        # history in the prompt. If the message itself contains no real
        # signal for switching topics, prefer staying with whichever
        # agent handled the last turn over trusting a possibly-flaky
        # classification -- see _topic_signal's docstring.
        last_agent = _load_last_agent(store_id, from_number)
        if history and last_agent and agent != last_agent and _topic_signal(text) is None:
            logger.info(
                "internal.routing: continuity override store=%d %s->%s (no topic signal) from=%s",
                store_id, agent, last_agent, from_number,
            )
            agent = last_agent

        logger.info(
            "internal.routing: store=%d agent=%s command=%s natural=%s from=%s",
            store_id, agent, command, is_natural, from_number,
        )

        if agent == "scout":
            # Scout should have been caught by async dispatch upstream; this is the edge-case fallback.
            reply = _scout(store_id, from_number, text, history=history)

        elif agent == "revenue":
            # Natural language goes straight to answer_question() -- one LLM
            # call given real computed numbers and the actual question,
            # mirroring scout/reputation/customer's proven-good single-pass
            # pattern. Previously this always went through handle_message()'s
            # classify-into-one-of-11-fixed-intents-then-template path (whose
            # own classifier ran on a keyword-regex fallback with no LLM at
            # all -- see registry.get_registry()) and only got fixed up
            # afterward via _adapt_response, which was working from a generic
            # template's text rather than the real data. Exact shorthand
            # commands (is_natural=False) still use the existing template path.
            if is_natural:
                reply = _revenue_answer(store_id, text, history=history)
            else:
                reply = _revenue(store_id, from_number, text)

        elif agent == "reputation":
            # "check" is the one command with no useful modifiers -- always
            # canonicalize it so any check-shaped phrasing reaches
            # reputation.py's exact-match fast path reliably. positive/
            # negative/reviews used to get canonicalized the same way, but
            # that silently discarded a time modifier the original phrasing
            # might carry ("show me LAST WEEK'S positive reviews") -- by the
            # time reputation.py saw just the bare word "positive", the "last
            # week" was already gone with no way to recover it downstream.
            # Passing the original text through instead means simple phrasings
            # that don't exactly match reputation.py's own trigger set (e.g.
            # "show me the good reviews") take its slightly slower
            # _classify_review_query fallback path instead of the instant
            # exact-match one -- an acceptable trade since that fallback is
            # already relied on for everything else and handles time-range
            # detection too.
            body_to_send = command if command == "check" else text
            reply = _reputation(store_id, from_number, body_to_send, history=history)

        elif agent == "maitre_d":
            reply = _maitre_d(store_id, from_number, text, history=history)

        else:
            # integrity (default)
            # Always pass the ORIGINAL text, never the classified command keyword.
            # IntegrityService.handle_message() splits on the first word to pick a
            # branch -- an exact shorthand command (is_natural=False) matches one
            # of its fixed branches directly and behaves exactly as before; a real
            # question's first word essentially never matches, so it naturally
            # falls through to the service's own free-form answer_question() (one
            # LLM call, real report data + the actual question) instead of the
            # fixed summary/leakage/profit templates. That free-form answer is
            # already a direct, tailored response, so no _adapt_response pass on
            # top of it.
            reply = _integrity(store_id, from_number, text, history=history)

    _save_staff_turn(store_id, from_number, history, text, reply)
    _save_last_agent(store_id, from_number, agent)
    return reply


def _integrity(store_id: int, from_number: str, text: str, history: list[dict] | None = None) -> str:
    try:
        from app.agents.integrity.service import get_service
        return get_service().handle_message(store_id, from_number, text, history=history)
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


def _revenue_answer(store_id: int, text: str, history: list[dict] | None = None) -> str:
    try:
        from app.agents.revenue.registry import get_registry
        return get_registry().answer_question(store_id, text, history=history)
    except Exception as e:
        logger.warning("internal._revenue_answer: store=%d error=%s", store_id, e)
        return "Revenue advisor is unavailable right now. Please try again shortly."


def _scout(store_id: int, from_number: str, text: str, history: list[dict] | None = None) -> str:
    """Fallback for scout-classified messages the upstream keyword-based
    async dispatch in gateway/main.py didn't catch -- e.g. natural-language
    queries like "what are other burger places doing" that contain none of
    _SCOUT_ASYNC_WORDS but still get classified as scout intent by the LLM
    here.

    Always answers from cache (answer_from_cache), never dispatches a live
    Apify scrape -- confirmed live this needs to be a hard rule, not just
    a freshness threshold: this used to call run("scout", ...) directly,
    which is live unconditionally, so every natural-language question (no
    matter how small) blocked the staff member for 7-45 min and spent real
    Apify credits just to answer something like "what's Burger Lab been up
    to". Live scraping is reserved for the explicit "scout" command (
    gateway/main.py's keyword dispatch) and the scheduled cron. Since this
    path never touches run()'s live branch, it also never writes a new
    Run/ScoutReport row, so a targeted single-competitor answer can no
    longer overwrite what a later plain "scout" request serves.
    """
    try:
        from app.agents.scout.pipeline import answer_from_cache
        return answer_from_cache(store_id, text, history=history)
    except Exception as e:
        logger.error("internal._scout: store=%d error=%s", store_id, e)
        return "Scout report could not be completed. Please try again."


def _reputation(store_id: int, from_number: str, text: str, history: list[dict] | None = None) -> str:
    try:
        from app.agents.reputation import process_reputation_owner_reply
        return process_reputation_owner_reply(from_number, text, store_id=store_id, history=history)
    except Exception as e:
        logger.warning("internal._reputation: store=%d error=%s", store_id, e)
        return (
            "Reviews agent is unavailable right now. Please try again shortly.\n\n"
            "Commands: *post* · *edit <text>* · *ignore* · *check*"
        )


def _maitre_d_door_shorthand(store_id: int, first: str, text: str) -> str | None:
    """seat/complete/noshow <id> — unambiguous door actions, skip the LLM
    entirely (same reasoning as reputation's post/edit/ignore). Returns
    None if this doesn't turn out to be a door command after all, so the
    caller can fall through to normal routing."""
    try:
        from app.agents.maitre_d.staff import handle_door_command
        words = text.split()
        rest = " ".join(words[1:]) if len(words) > 1 else ""
        return handle_door_command(store_id, first, rest)
    except Exception as e:
        logger.warning("internal._maitre_d_door_shorthand: store=%d error=%s", store_id, e)
        return None


def _maitre_d_listing(store_id: int, cmd: str) -> str:
    try:
        from app.agents.maitre_d.staff import format_reservations, format_waitlist, format_vips, format_locations
        if cmd == "waitlist":
            return format_waitlist(store_id)
        if cmd in ("vip", "vips", "vip list"):
            return format_vips(store_id)
        if cmd in ("locations", "branches"):
            return format_locations(store_id)
        return format_reservations(store_id)
    except Exception as e:
        logger.warning("internal._maitre_d_listing: store=%d error=%s", store_id, e)
        return "Reservations agent is unavailable right now. Please try again shortly."


def _maitre_d(store_id: int, from_number: str, text: str, history: list[dict] | None = None) -> str:
    """Natural-language questions about reservations/waitlist/VIPs that the
    router classified as maitre_d but didn't match a door-command/listing
    shorthand -- e.g. "who's booked in tonight", "any VIPs this week"."""
    try:
        from app.agents.maitre_d.staff import answer_question
        return answer_question(store_id, text, history=history)
    except Exception as e:
        logger.warning("internal._maitre_d: store=%d error=%s", store_id, e)
        return "Reservations agent is unavailable right now. Please try again shortly."
