"""Stamp logic — apply stamps, register members, status messages (multi-store)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.agents.customer.community.models import CommunityMember, StampEvent, VenueConfig
from app.agents.customer.community.store import append_stamp_event


@dataclass
class StampResult:
    message: str
    reward_issued: bool = False


def register_member(phone: str, name: str, members: dict[str, CommunityMember]) -> CommunityMember:
    now = datetime.now(timezone.utc).isoformat()
    m = CommunityMember(phone=phone, name=name, joined_at=now, last_activity_at=now)
    members[phone] = m
    return m


def apply_stamp(
    member: CommunityMember,
    code: str,
    config: VenueConfig,
    store_id: int,
) -> StampResult:
    now = datetime.now(timezone.utc).isoformat()
    member.stamps_current += 1
    member.stamps_lifetime += 1
    member.last_activity_at = now
    reward_issued = member.stamps_current >= config.stamp_goal
    event = StampEvent(
        phone=member.phone,
        code=code,
        stamp_number=member.stamps_current,
        reward_issued=reward_issued,
        at=now,
    )
    append_stamp_event(store_id, event)
    if reward_issued:
        member.stamps_current = 0
        return StampResult(
            message=(
                f"Stamp collected! 🎉\n\n"
                f"You've earned *{config.reward_text}*. "
                "Show this message to the cashier to claim your reward. "
                "Your stamps have been reset, keep collecting!"
            ),
            reward_issued=True,
        )
    remaining = config.stamp_goal - member.stamps_current
    return StampResult(
        message=(
            f"Stamp added! ✅\n\n"
            f"You're on *{member.stamps_current}/{config.stamp_goal}* stamps. "
            f"Just {remaining} more to earn {config.reward_text}. See you next time!"
        )
    )


def stamp_status_message(member: CommunityMember, config: VenueConfig) -> str:
    remaining = config.stamp_goal - member.stamps_current
    return (
        f"Your stamps: *{member.stamps_current}/{config.stamp_goal}* 🎫\n\n"
        f"{remaining} more stamp{'s' if remaining != 1 else ''} to earn {config.reward_text}.\n"
        f"Lifetime stamps collected: {member.stamps_lifetime}"
    )


def welcome_message(name: str, config: VenueConfig) -> str:
    return (
        f"Welcome to {config.venue_name}, {name}! 🎉\n\n"
        f"You're now part of our loyalty family. Collect *{config.stamp_goal} stamps* "
        f"and earn {config.reward_text}.\n\n"
        "Text a receipt code from the counter after your next visit to get your first stamp!"
    )


def welcome_back_message(name: str, member: CommunityMember, config: VenueConfig) -> str:
    remaining = config.stamp_goal - member.stamps_current
    return (
        f"Welcome back, {name}! 👋\n\n"
        f"You're on *{member.stamps_current}/{config.stamp_goal} stamps*, "
        f"just {remaining} more to earn {config.reward_text}. Keep it up!"
    )
