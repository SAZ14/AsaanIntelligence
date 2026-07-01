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
    load_venue_config, save_chat_session, save_members,
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

GREETING_RE = re.compile(r"^(hi|hello|hey|salam|assalam|aoa)\b", re.I)
STAMPS_RE = re.compile(r"\b(my stamps|stamp balance|how many stamps|stamps)\b", re.I)
LEADERBOARD_RE = re.compile(r"\b(leaderboard|top stamps|ranking)\b", re.I)
MENU_RE = re.compile(
    r"\b(menu|what.?s new|deals|special|price|recommend|latte|coffee|cake|croissant|mocha)\b",
    re.I,
)
_HOURS_RE = re.compile(r"\b(time|open|close|hour|timing|when|schedule)\b", re.I)
_LOCATION_RE = re.compile(r"\b(where|location|address|branch|find you|located)\b", re.I)
_DELIVERY_RE = re.compile(r"\b(deliver|delivery|order online|app)\b", re.I)

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
    return name


def _help_message(name: str, config: VenueConfig) -> str:
    return (
        f"Hi {name}! Here's what you can do 😊\n\n"
        f"- Text a *receipt code* (e.g. SR-AB12) to earn a stamp\n"
        f"- Send *my stamps* to check your progress\n"
        f"- Ask about the *menu* or current *deals*\n"
        f"- Send *leaderboard* to see this week's top collectors"
    )


def _chat_reply(
    user_message: str, context: str, member: CommunityMember,
    history: list[dict], store_id: int, phone: str,
    venue_name: str = "the restaurant",
) -> str:
    from app.core.llm import get_customer_model
    client = _get_client()
    if not client:
        return (
            f"Hi {member.name}! Ask me about the menu or deals, "
            "send *my stamps* to check your progress, or text a receipt code like SR-AB12 😊"
        )
    system_content = (
        f"You are a friendly team member at {venue_name} chatting on WhatsApp. "
        f"Guest name: {member.name or 'friend'}. Keep replies under 3 short sentences — "
        f"EXCEPT when listing menu items: list ALL items and prices from the context, do not cut the list short. "
        f"Be warm, natural, and conversational — like a real human, not a robot. "
        f"Naturally steer towards the menu, deals, or stamps.\n\n"
        f"WhatsApp formatting rules:\n"
        f"- Bold with *single asterisks* only, never **double**\n"
        f"- No markdown headers (no ##)\n"
        f"- No em-dashes — use a colon or comma instead\n"
        f"- Short sentences, 1-2 emojis max per reply\n\n"
        f"Content rules:\n"
        f"(1) ALWAYS use exact prices from the MENU — never guess or round.\n"
        f"(2) For location questions, copy branch names word-for-word from STORE KNOWLEDGE.\n"
        f"(3) For hours questions, state the exact open and close times from STORE KNOWLEDGE.\n"
        f"(4) For delivery questions, copy the EXACT platform name(s) from STORE KNOWLEDGE only — do not add any platform not mentioned there.\n"
        f"(5) When asked about the menu or specific items, LIST the items and prices directly from MENU context — never say 'I'll send a menu link' or suggest a link. There is no link.\n"
        f"(6) Never invent URLs, links, or information not present in the context below.\n\n{context}"
    )
    # Strip empty-content turns from history — empty assistant messages confuse the model
    clean_history = [m for m in history[-6:] if m.get("content")]
    messages = list(clean_history)
    messages.append({"role": "user", "content": user_message})

    def _call():
        return client.chat.completions.create(
            model=get_customer_model(),
            max_tokens=1000,
            messages=[{"role": "system", "content": system_content}] + messages,
        )

    import time as _time
    try:
        resp = _call()
    except Exception as exc:
        # Retry once on rate limit
        if "429" in str(exc) or "rate" in str(exc).lower():
            _time.sleep(8)
            resp = _call()
        else:
            raise

    reply = (resp.choices[0].message.content or "").strip()
    if not reply:
        return f"Sorry, I'm having trouble right now — try again in a moment 🙏"

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
        try:
            name = _clean_name(text)
        except ValueError as e:
            return AgentReply(str(e))
        register_member(phone, name, members)
        save_members(store_id, members)
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
        save_members(store_id, members)
        return AgentReply(result.message)

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

    if len(text) >= 3 and _get_client():
        from app.agents.customer.community.menu_context import build_menu_context
        ctx = build_menu_context(store_id)
        docs = search_knowledge_base(store_id, text, top_k=5)

        # Intent-based guaranteed injection — always include the right chunk for
        # hours/location/delivery so the model never has to guess.
        intent_types: list[str] = []
        if _HOURS_RE.search(text):
            intent_types.append("hours")
        if _LOCATION_RE.search(text):
            intent_types.append("location")
        if _DELIVERY_RE.search(text):
            intent_types.append("faq")
        if intent_types:
            extra = search_knowledge_base.__module__ and None  # just a marker
            from sqlalchemy import text as _sql
            from app.core.db import SessionLocal
            with SessionLocal() as _db:
                for chunk_type in intent_types:
                    row = _db.execute(
                        _sql("SELECT content FROM knowledge_base WHERE store_id = :sid AND metadata->>'type' = :t LIMIT 1"),
                        {"sid": store_id, "t": chunk_type},
                    ).fetchone()
                    if row:
                        guaranteed = {"content": row[0]}
                        if guaranteed not in docs:
                            docs.insert(0, guaranteed)

        if docs:
            ctx += "\n\nSTORE KNOWLEDGE:\n"
            for i, doc in enumerate(docs, 1):
                ctx += f"--- {i} ---\n{doc['content']}\n"
        history = load_chat_session(store_id, phone)
        return AgentReply(_chat_reply(text, ctx, member, history, store_id, phone, venue_name=config.venue_name))

    return AgentReply(_help_message(member.name, config))
