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
                f"Stamp #{event.stamp_number} added! You've earned {config.reward_text}. "
                "Show this to the cashier to claim it. Your stamps have been reset."
            ),
            reward_issued=True,
        )
    remaining = config.stamp_goal - member.stamps_current
    return StampResult(
        message=(
            f"Stamp #{event.stamp_number} added! "
            f"You have {member.stamps_current}/{config.stamp_goal} stamps. "
            f"{remaining} more to earn {config.reward_text}."
        )
    )


def stamp_status_message(member: CommunityMember, config: VenueConfig) -> str:
    remaining = config.stamp_goal - member.stamps_current
    return (
        f"You have {member.stamps_current}/{config.stamp_goal} stamps "
        f"({remaining} more for {config.reward_text}). "
        f"Lifetime stamps: {member.stamps_lifetime}."
    )


def welcome_message(name: str, config: VenueConfig) -> str:
    return (
        f"Welcome to {config.venue_name}, {name}! "
        f"Collect {config.stamp_goal} stamps to earn {config.reward_text}. "
        "Text a receipt code from the counter to get your first stamp."
    )


def welcome_back_message(name: str, member: CommunityMember, config: VenueConfig) -> str:
    remaining = config.stamp_goal - member.stamps_current
    return (
        f"Welcome back, {name}! You have {member.stamps_current}/{config.stamp_goal} stamps "
        f"({remaining} more for {config.reward_text})."
    )
