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
        raise ValueError("Please send your name (at least 2 characters).")
    if len(name) > 80:
        raise ValueError("That name is too long — please send a shorter one.")
    if is_redeem_code(name):
        raise ValueError("Please send your name, not a receipt code.")
    return name


def _help_message(name: str, config: VenueConfig) -> str:
    return (
        f"Hi {name}! You can:\n"
        f"* Text a receipt code (e.g. SR-AB12)\n"
        f"* Say 'my stamps' for your progress\n"
        f"* Say 'menu' or 'deals'\n"
        f"* Say 'leaderboard' for this week's top collectors"
    )


def _chat_reply(
    user_message: str, context: str, member: CommunityMember,
    history: list[dict], store_id: int, phone: str,
) -> str:
    from app.core.llm import get_model
    client = _get_client()
    if not client:
        return (
            f"Hi {member.name}! Ask me about the menu or deals, "
            "say 'my stamps', or text a receipt code like SR-AB12."
        )
    system_content = (
        f"You are a friendly employee at the cafe chatting on WhatsApp. "
        f"Guest name: {member.name or 'friend'}. Keep replies under 3 short sentences. "
        f"Be warm and conversational. Naturally steer towards the cafe, menu, deals, or stamps.\n\n"
        f"Rules: (1) ALWAYS use exact prices from the MENU. "
        f"(2) For location questions, copy the exact branch names and areas from STORE KNOWLEDGE word-for-word — never say 'Islamabad' alone when specific branches are listed. "
        f"(3) For hours questions, state the exact open/close times from STORE KNOWLEDGE.\n\n{context}"
    )
    messages = list(history[-6:])
    messages.append({"role": "user", "content": user_message})
    resp = client.chat.completions.create(
        model=get_model(),
        max_tokens=300,
        messages=[{"role": "system", "content": system_content}] + messages,
    )
    reply = resp.choices[0].message.content.strip()
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
            return AgentReply("What name should we use for your rewards?")
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
                "Please scan the counter QR to join the community first, then send your code."
            )
        entry = find_code(store_id, text)
        if entry is None:
            return AgentReply("That code wasn't found. Check the code on your receipt.")
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
            f"Welcome to {config.venue_name}! What name should we use for your rewards?"
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
        docs = search_knowledge_base(store_id, text, top_k=3)
        if docs:
            ctx += "\n\nSTORE KNOWLEDGE:\n"
            for i, doc in enumerate(docs, 1):
                ctx += f"--- {i} ---\n{doc['content']}\n"
        history = load_chat_session(store_id, phone)
        return AgentReply(_chat_reply(text, ctx, member, history, store_id, phone))

    return AgentReply(_help_message(member.name, config))
