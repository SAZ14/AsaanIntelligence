from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import anthropic

from app.analysis.retention import analyze_retention
from app.community.leaderboard import format_leaderboard, weekly_stamp_counts
from app.community.menu_context import build_menu_context
from app.community.store import load_members, load_venue_config
from app.ingest.loader import load_dataset
from app.services.messaging import normalize_phone, parse_twilio_whatsapp_phone, send_whatsapp_text

LOYAL_RE = re.compile(r"\b(loyal|top customer|best customer|regular)\b", re.I)
STATS_RE = re.compile(r"\b(how many|members|stats|community)\b", re.I)
LEADERBOARD_RE = re.compile(r"\bleaderboard\b", re.I)
MENU_RE = re.compile(r"\b(menu|what.?s new|deal|special)\b", re.I)


@dataclass
class AgentReply:
    body: str


def _is_owner(phone: str) -> bool:
    config = load_venue_config()
    normalized = normalize_phone(phone)
    owners = {normalize_phone(p) for p in config.owner_phones}
    env_owners = {
        normalize_phone(p.strip())
        for p in os.environ.get("ASAAN_OWNER_PHONES", "").split(",")
        if p.strip()
    }
    return normalized in owners or normalized in env_owners


def _loyal_community_summary(
    members_path: Path, sales_path: Path, menu_path: Path, staff_path: Path
) -> str:
    members = load_members()
    if members:
        top = sorted(
            members.values(),
            key=lambda m: (m.stamps_lifetime, m.stamps_current),
            reverse=True,
        )[:5]
        lines = ["Top community members by stamps:"]
        for m in top:
            lines.append(
                f"- {m.name or m.phone[-4:]}: {m.stamps_lifetime} lifetime stamps, "
                f"{m.stamps_current}/5 on current card"
            )
    else:
        lines = ["No community members yet."]

    orders, menu, staff = load_dataset(sales_path, menu_path, staff_path)
    ret = analyze_retention(orders, menu, staff)
    pos_top = sorted(ret.customers, key=lambda c: c.total_spend, reverse=True)[:3]
    if pos_top:
        lines.append("\nTop POS customers by spend:")
        for c in pos_top:
            lines.append(f"- {c.customer_ref}: {c.visit_count} visits, {c.total_spend:.0f} PKR")

    lines.append(f"\nTotal WhatsApp community members: {len(members)}")
    return "\n".join(lines)


def _stats_summary(events_path: Path | None = None) -> str:
    members = load_members()
    counts = weekly_stamp_counts()
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
    *,
    config_path: Path | None = None,
    members_path: Path | None = None,
    events_path: Path | None = None,
    menu_path: Path,
    deals_path: Path | None = None,
    sales_path: Path,
    staff_path: Path,
) -> AgentReply:
    phone = parse_twilio_whatsapp_phone(from_phone)
    if not _is_owner(phone):
        return AgentReply("This line is for Sugar Rush owners only.")

    text = (body or "").strip()
    if not text:
        return AgentReply(
            "Ask me about loyal customers, community stats, menu, deals, or leaderboard."
        )

    config = load_venue_config()

    if LOYAL_RE.search(text):
        return AgentReply(_loyal_community_summary(members_path or Path(), sales_path, menu_path, staff_path))
    if STATS_RE.search(text):
        return AgentReply(_stats_summary(events_path))
    if LEADERBOARD_RE.search(text):
        members = load_members()
        counts = weekly_stamp_counts()
        return AgentReply(format_leaderboard(counts, members))
    if MENU_RE.search(text):
        return AgentReply(build_menu_context(menu_path))

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return AgentReply(
            "Ask about loyal customers, community stats, menu, deals, or leaderboard."
        )
    context = (
        f"{_stats_summary(events_path)}\n\n"
        f"{_loyal_community_summary(members_path or Path(), sales_path, menu_path, staff_path)}\n\n"
        f"{build_menu_context(menu_path)}"
    )
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": (
                f"You are the owner assistant for {config.venue_name}. "
                f"Answer briefly using only this data:\n{context}\n\nOwner: {body}"
            ),
        }],
    )
    return AgentReply(resp.content[0].text.strip())


def process_merchant_reply(
    from_phone: str,
    body: str,
    *,
    menu_path: Path | None = None,
    sales_path: Path | None = None,
    staff_path: Path | None = None,
    use_twilio: bool | None = None,
) -> AgentReply:
    from app.api.deps import menu_path as default_menu_path
    from app.api.deps import sales_path as default_sales_path
    from app.api.deps import staff_path as default_staff_path
    reply = handle_merchant_message(
        from_phone, body,
        menu_path=menu_path or default_menu_path(),
        sales_path=sales_path or default_sales_path(),
        staff_path=staff_path or default_staff_path(),
    )
    send_whatsapp_text(
        from_phone, reply.body,
        from_key="TWILIO_WHATSAPP_MERCHANT_FROM",
        use_twilio=use_twilio,
    )
    return reply
