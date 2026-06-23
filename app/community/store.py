from __future__ import annotations

import csv
import json
from pathlib import Path

from app.community.models import CommunityMember, Deal, RedeemCode, StampEvent, VenueConfig

MEMBER_FIELDS = [
    "phone", "name", "stamps_current", "stamps_lifetime",
    "joined_at", "last_activity_at", "opted_in", "winback_sent_at",
]


def load_members(path: Path) -> dict[str, CommunityMember]:
    if not path.exists():
        return {}
    members: dict[str, CommunityMember] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            phone = row["phone"].strip()
            if not phone:
                continue
            members[phone] = CommunityMember(
                phone=phone,
                name=row.get("name", ""),
                stamps_current=int(row.get("stamps_current") or 0),
                stamps_lifetime=int(row.get("stamps_lifetime") or 0),
                joined_at=row.get("joined_at", ""),
                last_activity_at=row.get("last_activity_at", ""),
                opted_in=(row.get("opted_in", "true").lower() in ("1", "true", "yes")),
                winback_sent_at=row.get("winback_sent_at", ""),
            )
    return members


def save_members(path: Path, members: dict[str, CommunityMember]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MEMBER_FIELDS)
        writer.writeheader()
        for phone in sorted(members.keys()):
            m = members[phone]
            writer.writerow({
                "phone": m.phone,
                "name": m.name,
                "stamps_current": m.stamps_current,
                "stamps_lifetime": m.stamps_lifetime,
                "joined_at": m.joined_at,
                "last_activity_at": m.last_activity_at,
                "opted_in": "true" if m.opted_in else "false",
                "winback_sent_at": m.winback_sent_at,
            })


def load_redeem_codes(path: Path) -> list[RedeemCode]:
    if not path.exists():
        return []
    codes: list[RedeemCode] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            codes.append(RedeemCode.model_validate(json.loads(line)))
    return codes


def append_redeem_code(path: Path, code: RedeemCode) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(code.model_dump_json() + "\n")


def update_redeem_code(path: Path, code: RedeemCode) -> None:
    codes = load_redeem_codes(path)
    updated = [code if c.code == code.code else c for c in codes]
    path.write_text(
        "\n".join(c.model_dump_json() for c in updated) + ("\n" if updated else ""),
        encoding="utf-8",
    )


def load_stamp_events(path: Path) -> list[StampEvent]:
    if not path.exists():
        return []
    events: list[StampEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(StampEvent.model_validate(json.loads(line)))
    return events


def append_stamp_event(path: Path, event: StampEvent) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(event.model_dump_json() + "\n")


def load_venue_config(path: Path) -> VenueConfig:
    if not path.exists():
        return VenueConfig()
    return VenueConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))


def load_deals(path: Path) -> list[Deal]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [Deal.model_validate(d) for d in raw]


def load_onboarding_sessions(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_onboarding_sessions(path: Path, sessions: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sessions, indent=2), encoding="utf-8")
