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
    load_members,
    load_onboarding_sessions,
    load_venue_config,
    save_members,
    save_onboarding_sessions,
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


def _chat_reply(user_message: str, context: str, member_name: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return (
            f"Hi {member_name}! Ask me about the menu or deals, "
            "say 'my stamps', or text a receipt code like SR-AB12."
        )
    client = anthropic.Anthropic()
    prompt = (
        f"You are the friendly WhatsApp community agent for a café. "
        f"Guest name: {member_name or 'friend'}. Keep replies under 3 short sentences. "
        f"Only answer about the café menu, deals, stamps, and community. "
        f"If unsure, suggest they text a receipt code or say 'my stamps'.\n\n"
        f"{context}\n\nGuest: {user_message}"
    )
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def handle_customer_message(
    from_phone: str,
    body: str,
    *,
    members_path: Path,
    redeem_path: Path,
    events_path: Path,
    config_path: Path,
    deals_path: Path,
    menu_path: Path,
    sessions_path: Path,
) -> AgentReply:
    phone = parse_twilio_whatsapp_phone(from_phone)
    text = (body or "").strip()
    config = load_venue_config(config_path)
    members = load_members(members_path)
    sessions = load_onboarding_sessions(sessions_path)
    member = members.get(phone)

    if member is None and sessions.get(phone) == ONBOARDING:
        if not text:
            return AgentReply("What name should we use for your rewards?")
        try:
            name = _clean_name(text)
        except ValueError as e:
            return AgentReply(str(e))
        register_member(phone, name, members)
        save_members(members_path, members)
        sessions.pop(phone, None)
        save_onboarding_sessions(sessions_path, sessions)
        return AgentReply(welcome_message(name, config))

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
        result = apply_stamp(member, normalize_code(text), events_path, config)
        save_members(members_path, members)
        return AgentReply(result.message)

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

    if not text:
        return AgentReply(_help_message(member.name, config))

    if GREETING_RE.match(text):
        return AgentReply(welcome_back_message(member.name, member, config))
    if STAMPS_RE.search(text):
        return AgentReply(stamp_status_message(member, config))
    if LEADERBOARD_RE.search(text):
        counts = weekly_stamp_counts(events_path)
        return AgentReply(format_leaderboard(counts, members))
    if MENU_RE.search(text):
        ctx = build_menu_context(menu_path, deals_path)
        return AgentReply(_chat_reply(text, ctx, member.name))

    if len(text) > 20 and os.environ.get("ANTHROPIC_API_KEY"):
        ctx = build_menu_context(menu_path, deals_path)
        return AgentReply(_chat_reply(text, ctx, member.name))

    return AgentReply(_help_message(member.name, config))


def process_customer_reply(
    from_phone: str,
    body: str,
    paths: dict[str, Path],
    *,
    use_twilio: bool | None = None,
) -> AgentReply:
    reply = handle_customer_message(from_phone, body, **paths)
    send_whatsapp_text(
        from_phone, reply.body,
        from_key="TWILIO_WHATSAPP_CUSTOMER_FROM",
        use_twilio=use_twilio,
    )
    return reply
