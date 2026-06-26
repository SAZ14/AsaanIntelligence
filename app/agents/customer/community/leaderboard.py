"""Weekly stamp leaderboard (multi-store)."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.agents.customer.community.models import CommunityMember
from app.agents.customer.community.store import load_stamp_events


def weekly_stamp_counts(store_id: int, days: int = 7) -> dict[str, int]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    counts: dict[str, int] = defaultdict(int)
    for event in load_stamp_events(store_id):
        at = datetime.fromisoformat(event.at)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at >= cutoff:
            counts[event.phone] += 1
    return dict(counts)


def format_leaderboard(
    counts: dict[str, int],
    members: dict[str, CommunityMember],
    *,
    limit: int = 5,
    title: str = "This week's top stamp collectors",
) -> str:
    if not counts:
        return "No stamps collected yet this week. Be the first!"
    ranked = sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:limit]
    lines = [title + ":"]
    for i, (phone, count) in enumerate(ranked, 1):
        name = members.get(phone, CommunityMember(phone=phone)).name or phone[-4:]
        lines.append(f"{i}. {name} — {count} stamp{'s' if count != 1 else ''}")
    return "\n".join(lines)
