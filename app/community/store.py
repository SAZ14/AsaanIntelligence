from __future__ import annotations

from app.community.models import CommunityMember, Deal, RedeemCode, StampEvent, VenueConfig
from app.database import supabase


def load_members() -> dict[str, CommunityMember]:
    result = supabase.table("community_members").select("*").execute()
    members: dict[str, CommunityMember] = {}
    for row in result.data:
        m = CommunityMember(
            phone=row["phone"],
            name=row.get("name", ""),
            stamps_current=row.get("stamps_current", 0),
            stamps_lifetime=row.get("stamps_lifetime", 0),
            joined_at=row.get("joined_at", ""),
            last_activity_at=row.get("last_activity_at", ""),
            opted_in=row.get("opted_in", True),
            winback_sent_at=row.get("winback_sent_at") or "",
        )
        members[m.phone] = m
    return members


def save_members(members: dict[str, CommunityMember]) -> None:
    for m in members.values():
        supabase.table("community_members").upsert({
            "phone": m.phone,
            "name": m.name,
            "stamps_current": m.stamps_current,
            "stamps_lifetime": m.stamps_lifetime,
            "joined_at": m.joined_at,
            "last_activity_at": m.last_activity_at,
            "opted_in": m.opted_in,
            "winback_sent_at": m.winback_sent_at or None,
        }).execute()


def load_redeem_codes() -> list[RedeemCode]:
    result = supabase.table("redeem_codes").select("*").order("issued_at").execute()
    return [
        RedeemCode(
            code=r["code"],
            order_id=r.get("order_id", ""),
            issued_at=r["issued_at"],
            redeemed_at=r.get("redeemed_at") or "",
            redeemed_by=r.get("redeemed_by") or "",
        )
        for r in result.data
    ]


def append_redeem_code(code: RedeemCode) -> None:
    supabase.table("redeem_codes").insert({
        "code": code.code,
        "order_id": code.order_id,
        "issued_at": code.issued_at,
    }).execute()


def update_redeem_code(code: RedeemCode) -> None:
    supabase.table("redeem_codes").update({
        "redeemed_at": code.redeemed_at or None,
        "redeemed_by": code.redeemed_by or None,
    }).eq("code", code.code).execute()


def load_stamp_events() -> list[StampEvent]:
    result = supabase.table("stamp_events").select("*").order("at").execute()
    return [
        StampEvent(
            phone=r["phone"],
            code=r["code"],
            stamp_number=r["stamp_number"],
            reward_issued=r.get("reward_issued", False),
            at=r["at"],
        )
        for r in result.data
    ]


def append_stamp_event(event: StampEvent) -> None:
    supabase.table("stamp_events").insert({
        "phone": event.phone,
        "code": event.code,
        "stamp_number": event.stamp_number,
        "reward_issued": event.reward_issued,
        "at": event.at,
    }).execute()


def load_venue_config() -> VenueConfig:
    result = supabase.table("venue_config").select("*").limit(1).execute()
    if not result.data:
        return VenueConfig()
    row = result.data[0]
    return VenueConfig(
        venue_name=row.get("venue_name", "Sugar Rush"),
        stamp_goal=row.get("stamp_goal", 5),
        reward_text=row.get("reward_text", "a free drink or dessert"),
        winback_days=row.get("winback_days", 5),
        code_expiry_days=row.get("code_expiry_days", 30),
        owner_phones=list(row.get("owner_phones") or []),
        qr_greeting=row.get("qr_greeting", ""),
    )


def load_deals() -> list[Deal]:
    result = supabase.table("deals").select("*").execute()
    return [
        Deal(
            title=r["title"],
            description=r["description"],
            active=r.get("active", True),
        )
        for r in result.data
    ]


def load_onboarding_sessions() -> dict[str, str]:
    result = supabase.table("onboarding_sessions").select("*").execute()
    return {r["phone"]: r["state"] for r in result.data}


def save_onboarding_sessions(sessions: dict[str, str]) -> None:
    supabase.table("onboarding_sessions").delete().neq("phone", "0").execute()
    if sessions:
        supabase.table("onboarding_sessions").insert(
            [{"phone": p, "state": s} for p, s in sessions.items()]
        ).execute()
