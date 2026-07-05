"""Owner/merchant WhatsApp agent — community analytics queries (multi-store).

POS/financial analysis is handled by the integrity agent on the internal channel.
This agent answers community-focused questions: member stats, leaderboard, deals.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.agents.customer.community.leaderboard import format_leaderboard, weekly_stamp_counts
from app.agents.customer.community.menu_context import build_menu_context
from app.agents.customer.community.store import load_members, load_venue_config, search_knowledge_base
from app.agents.customer.services.messaging import normalize_phone, parse_twilio_whatsapp_phone

LOYAL_RE = re.compile(r"\b(loyal|top customer|best customer|regular)\b", re.I)
STATS_RE = re.compile(r"\b(how many|members|stats|community)\b", re.I)
LEADERBOARD_RE = re.compile(r"\bleaderboard\b", re.I)
MENU_RE = re.compile(r"\b(menu|what.?s new|deal|special)\b", re.I)

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


def _is_owner(phone: str, store_id: int) -> bool:
    config = load_venue_config(store_id)
    normalized = normalize_phone(phone)
    owners = {normalize_phone(p) for p in config.owner_phones}
    return normalized in owners


def _loyal_summary(store_id: int) -> str:
    members = load_members(store_id)
    if not members:
        return "No community members yet."
    top = sorted(
        members.values(),
        key=lambda m: (m.stamps_lifetime, m.stamps_current),
        reverse=True,
    )[:5]
    lines = ["Top community members by stamps:"]
    for m in top:
        lines.append(
            f"- {m.name or m.phone[-4:]}: {m.stamps_lifetime} lifetime, "
            f"{m.stamps_current} on current card"
        )
    lines.append(f"\nTotal members: {len(members)}")
    return "\n".join(lines)


def _stats_summary(store_id: int) -> str:
    members = load_members(store_id)
    counts = weekly_stamp_counts(store_id)
    return (
        f"Community members: {len(members)}\n"
        f"Active this week (earned stamps): {len(counts)}\n"
        f"Total lifetime stamps: {sum(m.stamps_lifetime for m in members.values())}\n"
        f"Members with stamps on card now: "
        f"{sum(1 for m in members.values() if m.stamps_current > 0)}"
    )


def handle_merchant_message(
    from_phone: str,
    body: str,
    store_id: int,
) -> AgentReply:
    from app.core.llm import get_model, nothink_kwargs
    phone = parse_twilio_whatsapp_phone(from_phone)
    if not _is_owner(phone, store_id):
        return AgentReply("This line is for restaurant owners only.")

    text = (body or "").strip()
    if not text:
        return AgentReply(
            "Ask me about community members, leaderboard, stats, or deals."
        )

    config = load_venue_config(store_id)
    members = load_members(store_id)

    if LOYAL_RE.search(text):
        return AgentReply(_loyal_summary(store_id))
    if STATS_RE.search(text):
        return AgentReply(_stats_summary(store_id))
    if LEADERBOARD_RE.search(text):
        counts = weekly_stamp_counts(store_id)
        return AgentReply(format_leaderboard(counts, members))
    if MENU_RE.search(text):
        return AgentReply(build_menu_context(store_id))

    client = _get_client()
    if not client:
        return AgentReply(
            "Ask about loyal customers, community stats, menu, or leaderboard."
        )

    context = (
        f"{_stats_summary(store_id)}\n\n"
        f"{_loyal_summary(store_id)}\n\n"
        f"{build_menu_context(store_id)}"
    )
    docs = search_knowledge_base(store_id, text, top_k=2)
    if docs:
        context += "\n\nSTORE KNOWLEDGE:\n"
        for i, doc in enumerate(docs, 1):
            context += f"--- {i} ---\n{doc['content']}\n"

    resp = client.chat.completions.create(
        model=get_model(),
        max_tokens=300,
        messages=[
            {"role": "system", "content": (
                f"You are the owner assistant for {config.venue_name}. "
                f"Answer briefly using only this data:\n{context}"
            )},
            {"role": "user", "content": text},
        ],
        **nothink_kwargs(get_model()),
    )
    reply = resp.choices[0].message.content.strip()
    return AgentReply(reply if reply else "I couldn't process that command.")
