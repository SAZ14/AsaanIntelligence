from __future__ import annotations

from app.community.leaderboard import format_leaderboard, weekly_stamp_counts
from app.community.store import load_members
from app.services.messaging import send_whatsapp_text


def run_leaderboard_broadcast(
    *,
    use_twilio: bool | None = None,
) -> int:
    members = load_members()
    counts = weekly_stamp_counts()
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
