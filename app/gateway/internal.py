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
    always the full command list across all four agents."""
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
        "You can also just write in plain language, e.g. \"how did we do this "
        "week\" or \"what are competitors offering\", no need to remember exact "
        "commands.\n\n"
        "Type *menu* to switch modes."
    )


def _get_store_name(store_id: int) -> str:
    from app.core.db import SessionLocal, Store
    with SessionLocal() as db:
        store = db.query(Store).filter(Store.id == store_id).first()
        return store.name if store else "your restaurant"


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


def _classify_with_llm(text: str) -> tuple[str, str]:
    """Return (agent, command) via ZAI. Falls back to keyword routing."""
    try:
        from app.core.llm import get_client, get_fast_model
        client = get_client()

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
            "scout/scout         – competitor intelligence, rival restaurants, what competitors are doing\n\n"
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
        )
        # Routing is a 25-token classification — the fast non-reasoning model
        # answers in <1s vs ~10s of thinking on glm-4.7 (verified 10/10 on an
        # English/Urdu routing eval before switching). Tight timeout: if the
        # API stalls, the keyword fallback below routes instead of making
        # staff wait out an API hiccup.
        resp = client.chat.completions.create(
            model=get_fast_model(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=25,
            timeout=8.0,
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
        from app.core.llm import get_client, get_fast_model
        try:
            client = get_client()
        except RuntimeError:
            return raw_response
        # Pure rewrite task — no reasoning needed, and the fast model keeps
        # numbers intact just as reliably (1.5s vs 17.7s measured live).
        # Timeout: the raw agent reply is already a complete answer, so a
        # stalled rewrite is never worth waiting for.
        resp = client.chat.completions.create(
            timeout=12.0,
            model=get_fast_model(),
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

    if not text or text.lower() in ("help", *_GREETINGS):
        return staff_help_text(_get_store_name(store_id))

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
            return _revenue_answer(store_id, text)
        return _revenue(store_id, from_number, text)

    if agent == "reputation":
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
        if command == "check":
            body_to_send = command
        else:
            body_to_send = text
        return _reputation(store_id, from_number, body_to_send)

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
    return _integrity(store_id, from_number, text)


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


def _revenue_answer(store_id: int, text: str) -> str:
    try:
        from app.agents.revenue.registry import get_registry
        return get_registry().answer_question(store_id, text)
    except Exception as e:
        logger.warning("internal._revenue_answer: store=%d error=%s", store_id, e)
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
    from app.gateway.main import _scout_rate_ok
    from app.core import cache as _cache
    from app.agents.scout.pipeline import scout_live_lock_key

    if not _scout_rate_ok(from_number):
        return "You've sent too many scout requests. Limit is 3 per hour, please wait before trying again."

    if _cache.is_locked(scout_live_lock_key(store_id)):
        return "Scout is already running, your report will arrive in a few minutes. Please wait."

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
