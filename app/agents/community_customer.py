from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import anthropic

from app.community.leaderboard import format_leaderboard, weekly_stamp_counts
from app.community.menu_context import build_menu_context
from app.community.stamps import (
    apply_stamp,
    register_member,
    stamp_status_message,
    welcome_back_message,
    welcome_message,
)
from app.community.store import (
    clear_onboarding_session,
    load_members,
    load_onboarding_sessions,
    load_venue_config,
    save_members,
    save_onboarding_sessions,
    load_chat_session,
    save_chat_session,
)
from app.community.tokens import find_code, is_redeem_code, mark_redeemed, normalize_code, validate_code
from app.services.messaging import parse_twilio_whatsapp_phone, send_whatsapp_text

ONBOARDING = "awaiting_name"

GREETING_RE = re.compile(r"^(hi|hello|hey|salam|assalam|aoa)\b", re.I)
STAMPS_RE = re.compile(r"\b(my stamps|stamp balance|how many stamps|stamps)\b", re.I)
LEADERBOARD_RE = re.compile(r"\b(leaderboard|top stamps|ranking)\b", re.I)
MENU_RE = re.compile(
    r"\b(menu|what.?s new|deals|special|price|recommend|latte|coffee|cake|croissant|mocha)\b",
    re.I,
)

# Module-level Anthropic client singleton — avoids re-instantiating per call.
_anthropic_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic | None:
    global _anthropic_client
    if _anthropic_client is None and os.environ.get("ANTHROPIC_API_KEY"):
        _anthropic_client = anthropic.Anthropic()
    return _anthropic_client


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


def _help_message(name: str, config) -> str:
    return (
        f"Hi {name}! You can:\n"
        f"• Text a receipt code (e.g. SR-AB12)\n"
        f"• Say 'my stamps' for your progress\n"
        f"• Say 'menu' or 'deals'\n"
        f"• Say 'leaderboard' for this week's top collectors"
    )


def _chat_reply(user_message: str, context: str, member: CommunityMember, history: list[dict], chat_sessions_path: Path | None) -> str:
    client = _get_client()
    if not client:
        return (
            f"Hi {member.name}! Ask me about the menu or deals, "
            "say 'my stamps', or text a receipt code like SR-AB12."
        )

    # Keep only the last 6 messages (3 turns) to manage context window
    messages = history[-6:]
    messages.append({"role": "user", "content": user_message})

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=(
            f"You are a friendly, human-like employee at the café chatting on WhatsApp. "
            f"Guest name: {member.name or 'friend'}. Keep replies under 3 short sentences. "
            f"Be conversational and warm. If they chat about general topics, be polite but "
            f"always naturally steer the conversation back to our café, coffee, menu, deals, or their loyalty stamps. "
            f"If they ask for help, suggest they text a receipt code or say 'my stamps'.\n\n"
            f"IMPORTANT: ALWAYS use the exact prices from the MENU provided below. If your conversational memory contradicts the MENU below, IGNORE your memory. The MENU below is the absolute truth.\n\n"
            f"{context}"
        ),
        messages=messages,
    )
    
    reply = resp.content[0].text.strip()
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": reply})
    save_chat_session(chat_sessions_path, member.phone, history)
    
    return reply


def handle_customer_message(
    from_phone: str,
    body: str,
    *,
    menu_path: Path | None = None,
    members_path: Path | None = None,
    redeem_path: Path | None = None,
    events_path: Path | None = None,
    config_path: Path | None = None,
    deals_path: Path | None = None,
    sessions_path: Path | None = None,
    chat_sessions_path: Path | None = None,
) -> AgentReply:
    phone = parse_twilio_whatsapp_phone(from_phone)
    text = (body or "").strip()
    config = load_venue_config(config_path)
    members = load_members(members_path)
    sessions = load_onboarding_sessions(sessions_path)
    member = members.get(phone)

    # ── Bug Fix #2: Respect opted_in — silently drop messages for opted-out members ──
    if member is not None and not member.opted_in:
        return AgentReply("")

    # ── Onboarding: awaiting name ──
    if member is None and sessions.get(phone) == ONBOARDING:
        if not text:
            return AgentReply("What name should we use for your rewards?")
        try:
            name = _clean_name(text)
        except ValueError as e:
            return AgentReply(str(e))
        register_member(phone, name, members)
        save_members(members_path, members)
        # Bug Fix #1: Use targeted clear instead of delete-all + re-insert
        clear_onboarding_session(sessions_path, phone)
        return AgentReply(welcome_message(name, config))

    # ── Receipt code redemption ──
    if is_redeem_code(text):
        if member is None:
            return AgentReply(
                "Please scan the counter QR to join the community first, then send your code."
            )
        entry = find_code(redeem_path, text)
        if entry is None:
            return AgentReply("That code wasn't found. Check the code on your receipt.")
        err = validate_code(entry, config)
        if err:
            return AgentReply(err)
        mark_redeemed(redeem_path, entry, phone)
        result = apply_stamp(member, normalize_code(text), config, events_path)
        save_members(members_path, members)
        return AgentReply(result.message)

    # ── New visitor: start onboarding ──
    if member is None:
        if not text:
            return AgentReply(
                f"Welcome to {config.venue_name}! What name should we use for your rewards?"
            )
        sessions[phone] = ONBOARDING
        save_onboarding_sessions(sessions_path, sessions)
        return AgentReply(
            f"Welcome to {config.venue_name}! You're on WhatsApp — we already have your number.\n"
            "What name should we use for your rewards?"
        )

    # ── Existing member intents ──
    if not text:
        return AgentReply(_help_message(member.name, config))

    if GREETING_RE.match(text):
        return AgentReply(welcome_back_message(member.name, member, config))
    if STAMPS_RE.search(text):
        return AgentReply(stamp_status_message(member, config))
    if LEADERBOARD_RE.search(text):
        counts = weekly_stamp_counts(events_path)
        return AgentReply(format_leaderboard(counts, members))
    
    # Send all other free text to the LLM agent, but only if it's long enough
    if len(text) >= 3 and _get_client():
        ctx = build_menu_context(menu_path, deals_path)
        history = load_chat_session(chat_sessions_path, phone)
        return AgentReply(_chat_reply(text, ctx, member, history, chat_sessions_path))

    return AgentReply(_help_message(member.name, config))


def process_customer_reply(
    from_phone: str,
    body: str,
    paths: dict | None = None,
    *,
    menu_path: Path | None = None,
    use_twilio: bool | None = None,
) -> AgentReply:
    """Send a WhatsApp reply to the customer.

    ``paths`` is an optional dict of path kwargs (used in tests).
    ``menu_path`` is the production shortcut (used by the webhook).
    """
    if paths is not None:
        # Test mode: paths dict contains all path overrides
        reply = handle_customer_message(from_phone, body, **paths)
    else:
        if os.environ.get("ASAAN_TEST_MODE") == "1":
            d = Path(os.environ.get("ASAAN_DATA_DIR", ""))
            paths = {
                "members_path": d / "community_members.csv",
                "redeem_path": d / "redeem_codes.jsonl",
                "events_path": d / "stamp_events.jsonl",
                "config_path": d / "venue_config.json",
                "deals_path": d / "deals.json",
                "menu_path": d / "menu.csv",
                "sessions_path": d / "onboarding_sessions.json",
                "chat_sessions_path": d / "chat_sessions.json",
            }
            reply = handle_customer_message(from_phone, body, **paths)
        else:
            reply = handle_customer_message(from_phone, body, menu_path=None)

    if reply.body:  # Don't send empty replies (opted-out members)
        send_whatsapp_text(
            from_phone, reply.body,
            from_key="TWILIO_WHATSAPP_CUSTOMER_FROM",
            use_twilio=use_twilio,
        )
    return reply
