from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.community.models import CommunityMember, StampEvent, VenueConfig
from app.community.store import append_stamp_event


@dataclass
class StampResult:
    member: CommunityMember
    stamp_number: int
    reward_issued: bool
    message: str


def register_member(phone: str, name: str, members: dict[str, CommunityMember]) -> CommunityMember:
    now = datetime.now(timezone.utc).isoformat()
    member = CommunityMember(
        phone=phone,
        name=name,
        joined_at=now,
        last_activity_at=now,
    )
    members[phone] = member
    return member


def welcome_back_message(name: str, member: CommunityMember, config: VenueConfig) -> str:
    status = stamp_status_message(member, config)
    return f"Welcome back, {name}! {status}"


def welcome_message(name: str, config: VenueConfig) -> str:
    return (
        f"Hi {name}, welcome to the {config.venue_name} community! "
        f"Text us your receipt code to collect stamps. "
        f"{config.stamp_goal} stamps = {config.reward_text}."
    )


def apply_stamp(
    member: CommunityMember,
    code: str,
    events_path: Path,
    config: VenueConfig,
) -> StampResult:
    now = datetime.now(timezone.utc).isoformat()
    member.stamps_current += 1
    member.stamps_lifetime += 1
    member.last_activity_at = now
    member.winback_sent_at = ""

    reward_issued = member.stamps_current >= config.stamp_goal
    stamp_number = member.stamps_current

    if reward_issued:
        message = (
            f"Stamp {config.stamp_goal}/{config.stamp_goal}! "
            f"You earned {config.reward_text}. Show this message at the counter. "
            f"Your stamp card resets — see you soon!"
        )
        member.stamps_current = 0
    else:
        remaining = config.stamp_goal - member.stamps_current
        message = (
            f"Stamp {stamp_number}/{config.stamp_goal} — "
            f"{remaining} more visit{'s' if remaining != 1 else ''} "
            f"for {config.reward_text}!"
        )

    append_stamp_event(events_path, StampEvent(
        phone=member.phone,
        code=code,
        stamp_number=stamp_number,
        reward_issued=reward_issued,
        at=now,
    ))
    return StampResult(
        member=member,
        stamp_number=stamp_number,
        reward_issued=reward_issued,
        message=message,
    )


def stamp_status_message(member: CommunityMember, config: VenueConfig) -> str:
    remaining = config.stamp_goal - member.stamps_current
    if member.stamps_current == 0:
        return (
            f"You're at 0/{config.stamp_goal} stamps. "
            f"Text your receipt code after your next visit!"
        )
    return (
        f"You have {member.stamps_current}/{config.stamp_goal} stamps — "
        f"{remaining} more for {config.reward_text}."
    )
