"""Multi-store Supabase persistence layer for the customer agent.

Every public function now takes store_id as the first argument to scope all
queries to the correct restaurant. Raw data isolation between stores is
enforced at the query level — no cross-store leakage is possible.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from app.agents.customer.community.models import (
    CommunityMember, Deal, RedeemCode, StampEvent, VenueConfig,
)


def _sb():
    from supabase import create_client
    url = os.environ["SUPABASE_URL"]
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_KEY"]
    return create_client(url, key)


# ── VenueConfig ──────────────────────────────────────────────────────────────

def load_venue_config(store_id: int) -> VenueConfig:
    result = _sb().table("venue_config").select("*").eq("store_id", store_id).maybe_single().execute()
    if not result or not result.data:
        # Fallback: load store name from stores table
        s = _sb().table("stores").select("name").eq("id", store_id).maybe_single().execute()
        name = s.data.get("name", "Restaurant") if s and s.data else "Restaurant"
        return VenueConfig(venue_name=name)
    row = result.data
    return VenueConfig(
        venue_name=row.get("venue_name", "Restaurant"),
        stamp_goal=row.get("stamp_goal", 5),
        reward_text=row.get("reward_text", "a free drink or dessert"),
        winback_days=row.get("winback_days", 5),
        code_expiry_days=row.get("code_expiry_days", 30),
        owner_phones=list(row.get("owner_phones") or []),
        qr_greeting=row.get("qr_greeting") or "",
    )


# ── CommunityMember ───────────────────────────────────────────────────────────

def load_members(store_id: int) -> dict[str, CommunityMember]:
    result = _sb().table("community_members").select("*").eq("store_id", store_id).execute()
    members = {}
    for row in result.data:
        m = CommunityMember(
            phone=row["phone"],
            name=row.get("name", ""),
            stamps_current=row.get("stamps_current", 0),
            stamps_lifetime=row.get("stamps_lifetime", 0),
            joined_at=row.get("joined_at") or "",
            last_activity_at=row.get("last_activity_at") or "",
            opted_in=row.get("opted_in", True),
            winback_sent_at=row.get("winback_sent_at") or "",
        )
        members[m.phone] = m
    return members


def save_members(store_id: int, members: dict[str, CommunityMember]) -> None:
    for m in members.values():
        _sb().table("community_members").upsert({
            "store_id": store_id,
            "phone": m.phone,
            "name": m.name,
            "stamps_current": m.stamps_current,
            "stamps_lifetime": m.stamps_lifetime,
            "joined_at": m.joined_at or None,
            "last_activity_at": m.last_activity_at or None,
            "opted_in": m.opted_in,
            "winback_sent_at": m.winback_sent_at or None,
        }).execute()


# ── RedeemCode ────────────────────────────────────────────────────────────────

def load_redeem_codes(store_id: int) -> list[RedeemCode]:
    result = _sb().table("redeem_codes").select("*").eq("store_id", store_id).order("issued_at").execute()
    return [
        RedeemCode(
            code=r["code"],
            order_id=r.get("order_id") or "",
            issued_at=r["issued_at"],
            redeemed_at=r.get("redeemed_at") or "",
            redeemed_by=r.get("redeemed_by") or "",
        )
        for r in result.data
    ]


def append_redeem_code(store_id: int, code: RedeemCode) -> None:
    _sb().table("redeem_codes").insert({
        "store_id": store_id,
        "code": code.code,
        "order_id": code.order_id or None,
        "issued_at": code.issued_at,
    }).execute()


def update_redeem_code(store_id: int, code: RedeemCode) -> None:
    _sb().table("redeem_codes").update({
        "redeemed_at": code.redeemed_at or None,
        "redeemed_by": code.redeemed_by or None,
    }).eq("store_id", store_id).eq("code", code.code).execute()


# ── StampEvent ────────────────────────────────────────────────────────────────

def load_stamp_events(store_id: int) -> list[StampEvent]:
    result = _sb().table("stamp_events").select("*").eq("store_id", store_id).order("at").execute()
    return [
        StampEvent(
            phone=r["phone"], code=r["code"],
            stamp_number=r["stamp_number"],
            reward_issued=r.get("reward_issued", False),
            at=r["at"],
        )
        for r in result.data
    ]


def append_stamp_event(store_id: int, event: StampEvent) -> None:
    _sb().table("stamp_events").insert({
        "store_id": store_id,
        "phone": event.phone,
        "code": event.code,
        "stamp_number": event.stamp_number,
        "reward_issued": event.reward_issued,
        "at": event.at,
    }).execute()


# ── Deals ─────────────────────────────────────────────────────────────────────

def load_deals(store_id: int) -> list[Deal]:
    result = _sb().table("deals").select("*").eq("store_id", store_id).execute()
    return [Deal(title=r["title"], description=r.get("description") or "", active=r.get("active", True)) for r in result.data]


# ── Onboarding Sessions ───────────────────────────────────────────────────────

def load_onboarding_sessions(store_id: int) -> dict[str, str]:
    result = _sb().table("onboarding_sessions").select("*").eq("store_id", store_id).execute()
    return {r["phone"]: r["state"] for r in result.data}


def save_onboarding_sessions(store_id: int, sessions: dict[str, str]) -> None:
    if sessions:
        _sb().table("onboarding_sessions").upsert(
            [{"store_id": store_id, "phone": p, "state": s} for p, s in sessions.items()]
        ).execute()


def clear_onboarding_session(store_id: int, phone: str) -> None:
    _sb().table("onboarding_sessions").delete().eq("store_id", store_id).eq("phone", phone).execute()


# ── Chat Sessions ─────────────────────────────────────────────────────────────

def load_chat_session(store_id: int, phone: str) -> list[dict]:
    result = (_sb().table("chat_sessions").select("history")
              .eq("store_id", store_id).eq("phone", phone).maybe_single().execute())
    if not result or not result.data:
        return []
    return result.data.get("history", [])


def save_chat_session(store_id: int, phone: str, history: list[dict]) -> None:
    _sb().table("chat_sessions").upsert({
        "store_id": store_id,
        "phone": phone,
        "history": history,
    }).execute()


# ── Knowledge Base (RAG) ──────────────────────────────────────────────────────

def search_knowledge_base(store_id: int, query: str, top_k: int = 3) -> list[dict]:
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("all-MiniLM-L6-v2")
        embedding = model.encode(query).tolist()
        res = _sb().rpc("match_knowledge_chunks", {
            "query_embedding": embedding,
            "match_threshold": 0.25,
            "match_count": top_k,
            "store_id_filter": store_id,
        }).execute()
        return res.data
    except Exception:
        return []


def store_knowledge_chunks(store_id: int, documents: list[dict]) -> None:
    """Embed and upsert knowledge chunks into Supabase knowledge_base table."""
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("all-MiniLM-L6-v2")
        rows = []
        for doc in documents:
            embedding = model.encode(doc["content"]).tolist()
            rows.append({
                "store_id": store_id,
                "content": doc["content"],
                "embedding": embedding,
                "metadata": doc.get("metadata", {}),
            })
        if rows:
            _sb().table("knowledge_base").insert(rows).execute()
    except Exception:
        pass


def clear_knowledge_by_source(store_id: int, source: str) -> None:
    """Remove knowledge chunks for a given source document."""
    try:
        _sb().table("knowledge_base").delete().eq("store_id", store_id).eq(
            "metadata->>source", source
        ).execute()
    except Exception:
        pass
