from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.community.store import load_members, load_venue_config, save_members
from app.services.messaging import send_whatsapp_text


def run_winback(
    *,
    use_twilio: bool | None = None,
) -> int:
    config = load_venue_config()
    members = load_members()
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.winback_days)
    sent = 0
    for member in members.values():
        if not member.opted_in or not member.last_activity_at:
            continue
        last = datetime.fromisoformat(member.last_activity_at)
        if last >= cutoff:
            continue
        if member.winback_sent_at:
            winback_at = datetime.fromisoformat(member.winback_sent_at)
            if winback_at >= cutoff:
                continue
        name = member.name or "friend"
        msg = f"Hi {name}, we miss you at {config.venue_name}! Come back soon and collect your next stamp."
        send_whatsapp_text(
            member.phone, msg,
            from_key="TWILIO_WHATSAPP_CUSTOMER_FROM",
            use_twilio=use_twilio,
        )
        member.winback_sent_at = datetime.now(timezone.utc).isoformat()
        sent += 1
    save_members(members)
    return sent
