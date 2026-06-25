from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.community.store import (
    load_members, load_venue_config, save_members,
    load_chat_session, save_chat_session,
)
from app.services.messaging import send_whatsapp_text


def run_winback(
    members_path: Path | None = None,
    config_path: Path | None = None,
    chat_sessions_path: Path | None = None,
    *,
    use_twilio: bool | None = None,
) -> int:
    """Send win-back WhatsApp messages to lapsed members.

    Args:
        members_path: Path to community_members.csv (None = use Supabase).
        config_path:  Path to venue_config.json    (None = use Supabase).
        use_twilio:   Force Twilio on/off; None = auto-detect from env.

    Returns:
        Number of messages sent.
    """
    config = load_venue_config(config_path)
    members = load_members(members_path)
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.winback_days)
    sent = 0

    for member in members.values():
        if not member.opted_in or not member.last_activity_at:
            continue
        last = datetime.fromisoformat(member.last_activity_at)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last >= cutoff:
            continue
        # Skip if we already sent a win-back recently
        if member.winback_sent_at:
            winback_at = datetime.fromisoformat(member.winback_sent_at)
            if winback_at.tzinfo is None:
                winback_at = winback_at.replace(tzinfo=timezone.utc)
            if winback_at >= cutoff:
                continue

        name = member.name or "friend"
        msg = (
            f"Hi {name}, we miss you at {config.venue_name}! "
            f"You have {member.stamps_current}/{config.stamp_goal} stamps waiting. "
            f"Come in and text your receipt code to keep collecting!"
        )
        send_whatsapp_text(
            member.phone, msg,
            from_key="TWILIO_WHATSAPP_CUSTOMER_FROM",
            use_twilio=use_twilio,
        )
        
        # Append proactive message to memory so the LLM agent has context
        history = load_chat_session(chat_sessions_path, member.phone)
        history.append({"role": "assistant", "content": msg})
        # Keep only the last 6 messages (same as agent)
        history = history[-6:]
        save_chat_session(chat_sessions_path, member.phone, history)

        member.winback_sent_at = datetime.now(timezone.utc).isoformat()
        sent += 1

    save_members(members_path, members)
    return sent
