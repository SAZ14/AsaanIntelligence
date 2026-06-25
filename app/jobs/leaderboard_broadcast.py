from __future__ import annotations

from pathlib import Path

from app.community.leaderboard import format_leaderboard, weekly_stamp_counts
from app.community.store import load_members
from app.services.messaging import send_whatsapp_text


def run_leaderboard_broadcast(
    members_path: Path | None = None,
    events_path: Path | None = None,
    *,
    use_twilio: bool | None = None,
) -> int:
    """Send weekly leaderboard broadcast to all opted-in members."""
    members = load_members(members_path)
    counts = weekly_stamp_counts(events_path)
    message = format_leaderboard(counts, members)
    sent = 0
    for member in members.values():
        if not member.opted_in:
            continue
        send_whatsapp_text(
            member.phone, message,
            from_key="TWILIO_WHATSAPP_CUSTOMER_FROM",
            use_twilio=use_twilio,
        )
        sent += 1
    return sent
