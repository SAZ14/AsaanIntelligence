"""Multi-store customer-facing WhatsApp agent.

All state is scoped by store_id so one central server can handle
messages for every restaurant simultaneously.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.agents.customer.community.models import CommunityMember, VenueConfig
from app.agents.customer.community.store import (
    append_stamp_event, clear_onboarding_session,
    load_chat_session, load_members, load_onboarding_sessions,
    load_venue_config, save_chat_session, save_member, save_members,
    save_onboarding_sessions, search_knowledge_base,
)
from app.agents.customer.community.stamps import (
    apply_stamp, register_member, stamp_status_message,
    welcome_back_message, welcome_message,
)
from app.agents.customer.community.tokens import (
    find_code, is_redeem_code, mark_redeemed, normalize_code, validate_code,
)
from app.agents.customer.services.messaging import parse_twilio_whatsapp_phone

ONBOARDING = "awaiting_name"

# Single common words that are clearly not names — caught before _clean_name
_NON_NAME_WORDS = frozenset({
    "menu", "hi", "hello", "hey", "salam", "aoa", "food", "burger", "burgers",
    "chicken", "beef", "deliver", "delivery", "order", "price", "prices", "deal",
    "deals", "yes", "no", "ok", "okay", "sure", "thanks", "thank", "please",
    "what", "how", "when", "where", "can", "do", "is", "are", "nothing",
})

GREETING_RE = re.compile(r"^(hi|hello|hey|salam|assalam|aoa)\b", re.I)
STAMPS_RE = re.compile(r"\b(my stamps|stamp balance|how many stamps|stamps)\b", re.I)
LEADERBOARD_RE = re.compile(r"\b(leaderboard|top stamps|ranking)\b", re.I)
# Menu intent detection lives in app.agents.customer.community.intent
# (embedding classifier, regex fallback) — see is_menu_intent() below.
_HOURS_RE = re.compile(r"\b(time|open(ing)?|clos(e|ing|ed)|hours?|timing|when|schedule)\b", re.I)
_LOCATION_RE = re.compile(r"\b(where|location|address|branches?|find you|located|outlet|outlets?)\b", re.I)
_DELIVERY_RE = re.compile(r"\b(deliver|delivery|order online|app)\b", re.I)
# Catches SR-prefixed text that LOOKS like a redeem code attempt but doesn't
# match the exact valid shape (wrong length, stray characters, etc.) — see
# the malformed-code check in handle_customer_message for why this matters.
_CODE_PREFIX_RE = re.compile(r"^SR-[A-Z0-9]*$", re.I)

_zai_client = None


def _get_client():
    global _zai_client
    if _zai_client is None:
        try:
            from app.core.llm import get_client
            _zai_client = get_client()
        except Exception:
            pass
    return _zai_client


@dataclass
class AgentReply:
    body: str


def _clean_name(raw: str) -> str:
    name = " ".join(raw.strip().split())
    if len(name) < 2:
        raise ValueError("Send us your name (at least 2 characters).")
    if len(name) > 80:
        raise ValueError("That name is a bit long. Could you send a shorter one?")
    if is_redeem_code(name):
        raise ValueError("That looks like a receipt code. What's your name?")
    lower = name.lower()
    if lower in _NON_NAME_WORDS:
        raise ValueError("Just your first name works great! What should we call you? 😊")
    if lower.startswith(("my name is ", "i am ", "i'm ", "call me ")):
        raise ValueError("Just your first name works great! What should we call you? 😊")
    return name


def _help_message(name: str, config: VenueConfig) -> str:
    return (
        f"Hi {name}! Here's what you can do 😊\n\n"
        f"- Text a *receipt code* (e.g. SR-AB12) to earn a stamp\n"
        f"- Send *my stamps* to check your progress\n"
        f"- Ask about the *menu* or current *deals*\n"
        f"- Send *leaderboard* to see this week's top collectors"
    )


def _fetch_kb_by_category(store_id: int, category: str) -> list[dict]:
    """Return all KB chunks for a store matching the given category (Redis-cached)."""
    import app.core.cache as _cache
    cache_key = f"kb:{store_id}:{category}"
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    from sqlalchemy import text as _sql
    from app.core.db import SessionLocal
    with SessionLocal() as db:
        rows = db.execute(
            _sql("SELECT content FROM knowledge_base WHERE store_id = :sid AND metadata->>'category' = :cat ORDER BY id"),
            {"sid": store_id, "cat": category},
        ).fetchall()
    docs = [{"content": r[0]} for r in rows]
    _cache.set(cache_key, docs, ttl=600)
    return docs


# Recognises any common currency prefix, not just Rs./PKR — a restaurant's
# KB may be priced in any currency, and a response quoting a *different*
# currency than the KB is itself a hallucination signal regardless of the
# number (this is how the $-menu hallucination slipped past an Rs.-only regex).
_CURRENCY_ALIASES = {
    "rs": "pkr", "rs.": "pkr", "pkr": "pkr", "₨": "pkr",
    "$": "usd", "usd": "usd",
    "€": "eur", "eur": "eur",
    "£": "gbp", "gbp": "gbp",
}
_PRICE_RE = re.compile(r"(Rs\.?|PKR|₨|\$|USD|€|£)\s*(\d[\d,]*(?:\.\d+)?)", re.I)
# Matches list-style "ItemName - <currency> NNN" lines the way the LLM is
# instructed to format menu answers, so an invented item paired with a real
# (but unrelated) price can be caught even though the bare price checks out.
_PRICE_LINE_RE = re.compile(
    r"^\s*[-*•]?\s*\*?([A-Za-z][\w'&/() ]{1,60}?)\*?\s*[-–—:]\s*(Rs\.?|PKR|₨|\$|USD|€|£)\s*(\d[\d,]*(?:\.\d+)?)",
    re.M | re.I,
)


def _norm_currency(symbol: str) -> str:
    return _CURRENCY_ALIASES.get(symbol.lower().rstrip("."), symbol.lower())


def _extract_prices(text: str) -> set[tuple[str, float]]:
    """Return the set of (currency, value) pairs mentioned in text.

    Exact numeric values, not substrings — "Rs. 12" must never match inside
    a real "Rs. 1250" just because "12" is a substring of "1250".
    """
    out: set[tuple[str, float]] = set()
    for symbol, num in _PRICE_RE.findall(text):
        try:
            value = float(num.replace(",", ""))
        except ValueError:
            continue
        out.add((_norm_currency(symbol), value))
    return out


def _prices_grounded(response: str, chunks: list[dict]) -> bool:
    """Return True only if every price the response cites, and every
    (item name, price) pair it lists, can be verified against the KB chunks.

    Three checks: (1) every (currency, value) pair in the response must
    exist somewhere in the KB — catches invented prices AND wrong currency
    (e.g. LLM quoting $ when the KB only ever quotes Rs.); (2) for
    list-style "Item - <currency> NNN" lines, that exact name and price
    must co-occur on the same KB line — catches an invented item name
    paired with a real, coincidentally matching price.
    """
    response_prices = _extract_prices(response)
    item_price_lines = _PRICE_LINE_RE.findall(response)
    if not response_prices and not item_price_lines:
        return True
    if not chunks:
        # Response cites prices/items but we retrieved no KB context to
        # verify them against — treat as ungrounded rather than trusting
        # the LLM blindly.
        return False

    chunk_text = " ".join(c["content"] for c in chunks)
    kb_prices = _extract_prices(chunk_text)
    if not kb_prices or not response_prices <= kb_prices:
        return False

    kb_lines = [line for c in chunks for line in c["content"].splitlines()]
    # Normalize KB lines the same way the extracted name is normalized (strip
    # punctuation like parens) -- otherwise "Milkshakes (Shake Relief)" never
    # matches its own KB line "Milkshakes (Shake Relief) - Rs. 520...", since
    # the name loses its parens but the line being searched still has them.
    kb_lines_norm = [
        (re.sub(r"[^a-z0-9' ]", "", line.lower()), line) for line in kb_lines
    ]
    for name, symbol, num in item_price_lines:
        name_norm = re.sub(r"[^a-z0-9' ]", "", name.strip().lower())
        if len(name_norm) < 3:
            continue
        try:
            value = float(num.replace(",", ""))
        except ValueError:
            continue
        target = (_norm_currency(symbol), value)
        if not any(
            name_norm in line_norm and target in _extract_prices(line)
            for line_norm, line in kb_lines_norm
        ):
            return False
    return True


def _sanitize_whatsapp_formatting(text: str) -> str:
    """Fix formatting the LLM occasionally emits despite being told not to.

    WhatsApp only renders *single-asterisk* bold — **double** shows up as
    literal asterisk characters in the chat, and markdown ## headers don't
    render at all. This is a post-generation safety net, not a substitute
    for the system prompt rules (which stay in place as the primary fix).
    """
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    return text


def _llm_generate(
    user_message: str, context: str, member: CommunityMember,
    history: list[dict], venue_name: str = "the restaurant",
) -> str:
    """Pure LLM call — returns response text only, no side effects."""
    from app.core.llm import get_customer_model
    client = _get_client()
    if not client:
        return ""
    system_content = (
        f"You are a friendly team member at {venue_name} chatting on WhatsApp. "
        f"Guest name: {member.name or 'friend'}. Keep replies under 3 short sentences, "
        f"EXCEPT when listing menu items: list ALL items and prices from the context, do not cut the list short. "
        f"Be warm, natural, and conversational, like a real human, not a robot. "
        f"Naturally steer towards the menu, deals, or stamps.\n\n"
        f"WhatsApp formatting rules:\n"
        f"- Bold with *single asterisks* only, never **double**\n"
        f"- No markdown headers (no ##)\n"
        f"- No em-dashes, use a colon or comma instead\n"
        f"- Short sentences. A small number of contextually relevant emojis are welcome "
        f"(e.g. 🍔 next to a burger, 🍕 next to pizza) -- max 3 per paragraph, never more, "
        f"and never just decoration with no connection to what's being said\n\n"
        f"Content rules:\n"
        f"(1) ALWAYS use exact prices from the MENU, never guess or round.\n"
        f"(2) For location questions, copy branch names word-for-word from STORE KNOWLEDGE.\n"
        f"(3) For hours questions, state the exact open and close times from STORE KNOWLEDGE.\n"
        f"(4) For delivery questions, copy the EXACT platform name(s) from STORE KNOWLEDGE only, do not add any platform not mentioned there.\n"
        f"(5) When asked about the menu or specific items, LIST the items and prices directly from MENU context, never say 'I'll send a menu link' or suggest a link. There is no link.\n"
        f"(6) Never invent URLs, links, or information not present in the context below.\n\n{context}"
    )
    clean_history = [m for m in history[-6:] if m.get("content")]
    messages = list(clean_history) + [{"role": "user", "content": user_message}]

    def _call():
        return client.chat.completions.create(
            model=get_customer_model(),
            max_tokens=1000,
            messages=[{"role": "system", "content": system_content}] + messages,
            timeout=20.0,
        )

    import time as _time
    try:
        resp = _call()
    except Exception as exc:
        if "429" in str(exc) or "rate" in str(exc).lower():
            _time.sleep(8)
            resp = _call()
        else:
            raise

    text = (resp.choices[0].message.content or "").strip()
    return _sanitize_whatsapp_formatting(text) if text else text


def _chat_reply(
    user_message: str, context: str, member: CommunityMember,
    history: list[dict], store_id: int, phone: str,
    venue_name: str = "the restaurant",
) -> str:
    """LLM call + history save. Use _llm_generate directly when validation is needed."""
    reply = _llm_generate(user_message, context, member, history, venue_name)
    if not reply:
        return "Sorry, I'm having trouble right now, try again in a moment 🙏"
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": reply})
    save_chat_session(store_id, phone, history[-6:])
    return reply


def handle_customer_message(
    from_phone: str,
    body: str,
    store_id: int,
) -> AgentReply:
    phone = parse_twilio_whatsapp_phone(from_phone)
    text = (body or "").strip()
    config = load_venue_config(store_id)
    members = load_members(store_id)
    sessions = load_onboarding_sessions(store_id)
    member = members.get(phone)

    if member is not None and not member.opted_in:
        return AgentReply("")

    # Onboarding: awaiting name
    if member is None and sessions.get(phone) == ONBOARDING:
        if not text:
            return AgentReply("What name should we put on your rewards? 😊")
        # If the message looks like a question/statement rather than a name,
        # re-prompt clearly instead of showing a confusing validation error.
        words = text.split()
        if (
            len(text) > 50 or "?" in text or len(words) > 4
            or any(w.lower().strip(".,!") in _NON_NAME_WORDS for w in words)
        ):
            return AgentReply("Just your first name is perfect! 😊 What should we call you?")
        try:
            name = _clean_name(text)
        except ValueError as e:
            return AgentReply(str(e))
        register_member(phone, name, members)
        save_member(store_id, members[phone])
        clear_onboarding_session(store_id, phone)
        return AgentReply(welcome_message(name, config))

    # Receipt code
    if is_redeem_code(text):
        if member is None:
            return AgentReply(
                "Scan the QR code at the counter to join our loyalty programme first, then send your code 😊"
            )
        entry = find_code(store_id, text)
        if entry is None:
            return AgentReply("That code wasn't found. Double-check the code on your receipt 🧾")
        err = validate_code(entry, config)
        if err:
            return AgentReply(err)
        mark_redeemed(store_id, entry, phone)
        result = apply_stamp(member, normalize_code(text), config, store_id)
        save_member(store_id, member)
        return AgentReply(result.message)

    # Malformed code attempt (right prefix, wrong shape) — must be caught
    # here too, not just exact matches. Otherwise it falls through to the
    # LLM, which has no way to know a code is invalid and can hallucinate
    # an order confirmation for a code that was never actually redeemed.
    if _CODE_PREFIX_RE.match(text.strip()):
        return AgentReply("That code wasn't found. Double-check the code on your receipt 🧾")

    # New visitor: start onboarding
    if member is None:
        sessions[phone] = ONBOARDING
        save_onboarding_sessions(store_id, sessions)
        return AgentReply(
            f"Hey! Welcome to {config.venue_name} 👋\n\n"
            "What name should we put on your loyalty rewards?"
        )

    # Existing member
    if not text:
        return AgentReply(_help_message(member.name, config))

    if GREETING_RE.match(text):
        return AgentReply(welcome_back_message(member.name, member, config))
    if STAMPS_RE.search(text):
        return AgentReply(stamp_status_message(member, config))
    if LEADERBOARD_RE.search(text):
        from app.agents.customer.community.leaderboard import format_leaderboard, weekly_stamp_counts
        counts = weekly_stamp_counts(store_id)
        return AgentReply(format_leaderboard(counts, members))

    if len(text) >= 3:
        from app.agents.customer.community.menu_context import build_menu_context
        from app.agents.customer.community.intent import is_menu_intent

        is_menu     = is_menu_intent(text)
        is_hours    = bool(_HOURS_RE.search(text))
        is_location = bool(_LOCATION_RE.search(text))
        is_delivery = bool(_DELIVERY_RE.search(text))

        # Pure factual queries (location / hours / delivery, no menu) don't
        # need the LLM — the KB chunk IS the answer.  For everything else,
        # bail out early if no LLM client is configured so we never touch the DB.
        only_factual = (is_hours or is_location or is_delivery) and not is_menu
        if not only_factual and not _get_client():
            return AgentReply(_help_message(member.name, config))

        # Fetch intent-specific KB chunks by standardised category field.
        # The 'category' key is set during KB seeding and is restaurant-agnostic.
        # Menu content is always fetched (not gated on is_menu) — keyword/phrase
        # regexes can't catch every phrasing or language ("bhook lagrahi hai
        # mujhe" == "I'm hungry" in Urdu), so relying on intent-matching alone
        # to decide whether to ground the LLM in real menu data left vague or
        # non-English hunger/food questions with zero real context, and the
        # LLM would invent a full menu (wrong items, wrong currency) rather
        # than say it doesn't know. Redis-cached, so this is cheap.
        intent_docs: dict[str, list[dict]] = {"menu": _fetch_kb_by_category(store_id, "menu")}
        if is_hours:
            intent_docs["hours"] = _fetch_kb_by_category(store_id, "hours")
        if is_location:
            intent_docs["location"] = _fetch_kb_by_category(store_id, "location")
        if is_delivery:
            intent_docs["delivery"] = _fetch_kb_by_category(store_id, "delivery")

        # ── Direct format for pure factual intents (no LLM needed) ───────────
        if only_factual:
            parts: list[str] = []
            for cat in ("location", "hours", "delivery"):
                for doc in intent_docs.get(cat, []):
                    if doc["content"] not in parts:
                        parts.append(doc["content"])
            if parts:
                reply = "\n\n".join(parts)
                history = load_chat_session(store_id, phone)
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": reply})
                save_chat_session(store_id, phone, history[-6:])
                return AgentReply(reply)
            # No KB data tagged for this intent yet — fall through to LLM

        # ── LLM path for menu + conversational queries ────────────────────────
        if not _get_client():
            return AgentReply(_help_message(member.name, config))

        # Build context: vector search + all intent-specific chunks
        docs = search_knowledge_base(store_id, text, top_k=5)
        seen_content: set[str] = {d["content"] for d in docs}
        for cat_docs in intent_docs.values():
            for d in cat_docs:
                if d["content"] not in seen_content:
                    docs.insert(0, d)
                    seen_content.add(d["content"])

        ctx = build_menu_context(store_id)
        if docs:
            ctx += "\n\nSTORE KNOWLEDGE:\n"
            for i, doc in enumerate(docs, 1):
                ctx += f"--- {i} ---\n{doc['content']}\n"

        history = load_chat_session(store_id, phone)
        try:
            reply = _llm_generate(text, ctx, member, history, venue_name=config.venue_name)
        except Exception as exc:
            # LLM timed out or errored — answer from the KB directly rather
            # than leaving the customer with no reply at all.
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "customer.llm_generate failed store=%d: %s — serving KB fallback", store_id, exc
            )
            if docs:
                reply = "\n\n".join(d["content"] for d in docs[:2])
            else:
                reply = f"Sorry {member.name}, I'm having trouble right now, please try again in a moment 🙏"
            return AgentReply(reply)

        if not reply:
            return AgentReply(_help_message(member.name, config))

        # ── Price validation: every price in the reply must exist in KB ───────
        if not _prices_grounded(reply, docs):
            # LLM invented prices — fall back to direct KB content
            if intent_docs.get("menu"):
                reply = "\n\n".join(d["content"] for d in intent_docs["menu"])
            else:
                reply = f"Let me make sure I give you accurate info, {member.name}! Reach out to us directly for details 😊"

        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": reply})
        save_chat_session(store_id, phone, history[-6:])
        return AgentReply(reply)

    return AgentReply(_help_message(member.name, config))
