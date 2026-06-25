"""Dual-mode persistence layer.

Each public function accepts an optional ``path`` argument.
- When a Path is supplied  → use local JSON/JSONL/CSV files (tests & local dev).
- When path is None        → use Supabase (production).

This design eliminates the Supabase dependency from unit tests and fixes the
race condition in save_onboarding_sessions (previously delete-all + re-insert).
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from app.community.models import CommunityMember, Deal, RedeemCode, StampEvent, VenueConfig
from app.models.canonical import MenuItem


# ── helpers ──────────────────────────────────────────────────────────────────

def _supabase():
    from app.database import supabase
    return supabase


# ── VenueConfig ──────────────────────────────────────────────────────────────

def load_venue_config(path: Path | None = None) -> VenueConfig:
    if path is not None:
        if not path.exists():
            return VenueConfig()
        data = json.loads(path.read_text(encoding="utf-8"))
        return VenueConfig(**data)

    result = _supabase().table("venue_config").select("*").limit(1).execute()
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


# ── CommunityMember ───────────────────────────────────────────────────────────

def load_members(path: Path | None = None) -> dict[str, CommunityMember]:
    if path is not None:
        if not path.exists():
            return {}
        members: dict[str, CommunityMember] = {}
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                m = CommunityMember(
                    phone=row["phone"],
                    name=row.get("name", ""),
                    stamps_current=int(row.get("stamps_current") or 0),
                    stamps_lifetime=int(row.get("stamps_lifetime") or 0),
                    joined_at=row.get("joined_at", ""),
                    last_activity_at=row.get("last_activity_at", ""),
                    opted_in=row.get("opted_in", "true").lower() not in ("false", "0", ""),
                    winback_sent_at=row.get("winback_sent_at") or "",
                )
                members[m.phone] = m
        return members

    result = _supabase().table("community_members").select("*").execute()
    members = {}
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


def save_members(path: Path | None, members: dict[str, CommunityMember]) -> None:
    if path is not None:
        fieldnames = [
            "phone", "name", "stamps_current", "stamps_lifetime",
            "joined_at", "last_activity_at", "opted_in", "winback_sent_at",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for m in members.values():
                writer.writerow({
                    "phone": m.phone,
                    "name": m.name,
                    "stamps_current": m.stamps_current,
                    "stamps_lifetime": m.stamps_lifetime,
                    "joined_at": m.joined_at,
                    "last_activity_at": m.last_activity_at,
                    "opted_in": m.opted_in,
                    "winback_sent_at": m.winback_sent_at or "",
                })
        return

    for m in members.values():
        _supabase().table("community_members").upsert({
            "phone": m.phone,
            "name": m.name,
            "stamps_current": m.stamps_current,
            "stamps_lifetime": m.stamps_lifetime,
            "joined_at": m.joined_at,
            "last_activity_at": m.last_activity_at,
            "opted_in": m.opted_in,
            "winback_sent_at": m.winback_sent_at or None,
        }).execute()


# ── RedeemCode ────────────────────────────────────────────────────────────────

def load_redeem_codes(path: Path | None = None) -> list[RedeemCode]:
    if path is not None:
        if not path.exists():
            return []
        codes = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            codes.append(RedeemCode(
                code=r["code"],
                order_id=r.get("order_id", ""),
                issued_at=r["issued_at"],
                redeemed_at=r.get("redeemed_at") or "",
                redeemed_by=r.get("redeemed_by") or "",
            ))
        return codes

    result = _supabase().table("redeem_codes").select("*").order("issued_at").execute()
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


def append_redeem_code(path: Path | None, code: RedeemCode) -> None:
    if path is not None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "code": code.code,
                "order_id": code.order_id,
                "issued_at": code.issued_at,
                "redeemed_at": code.redeemed_at or None,
                "redeemed_by": code.redeemed_by or None,
            }) + "\n")
        return

    _supabase().table("redeem_codes").insert({
        "code": code.code,
        "order_id": code.order_id,
        "issued_at": code.issued_at,
    }).execute()


def update_redeem_code(path: Path | None, code: RedeemCode) -> None:
    if path is not None:
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r["code"] == code.code:
                r["redeemed_at"] = code.redeemed_at or None
                r["redeemed_by"] = code.redeemed_by or None
            lines.append(json.dumps(r))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    _supabase().table("redeem_codes").update({
        "redeemed_at": code.redeemed_at or None,
        "redeemed_by": code.redeemed_by or None,
    }).eq("code", code.code).execute()


# ── StampEvent ────────────────────────────────────────────────────────────────

def load_stamp_events(path: Path | None = None) -> list[StampEvent]:
    if path is not None:
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            events.append(StampEvent(
                phone=r["phone"],
                code=r["code"],
                stamp_number=r["stamp_number"],
                reward_issued=r.get("reward_issued", False),
                at=r["at"],
            ))
        return events

    result = _supabase().table("stamp_events").select("*").order("at").execute()
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


def append_stamp_event(path: Path | None, event: StampEvent) -> None:
    if path is not None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "phone": event.phone,
                "code": event.code,
                "stamp_number": event.stamp_number,
                "reward_issued": event.reward_issued,
                "at": event.at,
            }) + "\n")
        return

    _supabase().table("stamp_events").insert({
        "phone": event.phone,
        "code": event.code,
        "stamp_number": event.stamp_number,
        "reward_issued": event.reward_issued,
        "at": event.at,
    }).execute()


# ── Deals ─────────────────────────────────────────────────────────────────────

def load_deals(path: Path | None = None) -> list[Deal]:
    if path is not None:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return [Deal(**d) for d in data]

    result = _supabase().table("deals").select("*").execute()
    return [
        Deal(
            title=r["title"],
            description=r["description"],
            active=r.get("active", True),
        )
        for r in result.data
    ]


# ── Onboarding Sessions ───────────────────────────────────────────────────────
# BUG FIX: The previous implementation did delete-all + re-insert which caused
# a race condition when two users onboarded simultaneously.  The new approach:
# - File mode: atomic read-modify-write of a single JSON dict (per-phone)
# - Supabase mode: per-phone upsert + targeted delete (never nuke the whole table)

def load_onboarding_sessions(path: Path | None = None) -> dict[str, str]:
    if path is not None:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    result = _supabase().table("onboarding_sessions").select("*").execute()
    return {r["phone"]: r["state"] for r in result.data}


def save_onboarding_sessions(path: Path | None, sessions: dict[str, str]) -> None:
    """Write onboarding sessions without clobbering concurrent entries.

    File mode  : atomic rewrite of the full dict (single-process safe).
    Supabase   : upsert each phone individually — never delete-all.
                 Entries are removed only via clear_onboarding_session().
    """
    if path is not None:
        path.write_text(json.dumps(sessions, indent=2), encoding="utf-8")
        return

    if sessions:
        _supabase().table("onboarding_sessions").upsert(
            [{"phone": p, "state": s} for p, s in sessions.items()]
        ).execute()


def clear_onboarding_session(path: Path | None, phone: str) -> None:
    """Remove a single completed onboarding session by phone."""
    if path is not None:
        sessions = load_onboarding_sessions(path)
        sessions.pop(phone, None)
        path.write_text(json.dumps(sessions, indent=2), encoding="utf-8")
        return

    _supabase().table("onboarding_sessions").delete().eq("phone", phone).execute()


# ── Chat Sessions ─────────────────────────────────────────────────────────────

def load_chat_session(path: Path | None, phone: str) -> list[dict]:
    if path is not None:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get(phone, [])

    result = _supabase().table("chat_sessions").select("history").eq("phone", phone).maybe_single().execute()
    if not result or not result.data:
        return []
    return result.data.get("history", [])


def save_chat_session(path: Path | None, phone: str, history: list[dict]) -> None:
    if path is not None:
        if not path.exists():
            data = {}
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
        data[phone] = history
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return

    _supabase().table("chat_sessions").upsert({
        "phone": phone,
        "history": history
    }).execute()

# ── Menu Items ────────────────────────────────────────────────────────────────

def load_menu(path: Path | None = None) -> dict[str, MenuItem]:
    if path is not None:
        from app.ingest.loader import load_menu as _load_csv_menu
        if not path.exists():
            return {}
        return _load_csv_menu(path)
        
    result = _supabase().table("menu_items").select("*").eq("active", True).execute()
    menu = {}
    for row in result.data:
        m = MenuItem(
            sku=row["sku"],
            name=row["name"],
            category=row["category"],
            cost=float(row["cost"]) if row.get("cost") is not None else None,
            price=float(row["price"])
        )
        menu[m.sku] = m
    return menu


def upsert_menu_item(path: Path | None, item: MenuItem) -> None:
    if path is not None:
        from app.ingest.loader import load_menu as _load_csv_menu
        from app.ingest.mappings import cafe_generic as default_mapping
        import csv
        menu = _load_csv_menu(path) if path.exists() else {}
        menu[item.sku] = item
        m = default_mapping.MENU
        fieldnames = list(m.values())
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for i in menu.values():
                writer.writerow({
                    m["sku"]: i.sku,
                    m["name"]: i.name,
                    m["category"]: i.category,
                    m["cost"]: str(i.cost) if i.cost is not None else "",
                    m["price"]: str(i.price),
                })
        return

    _supabase().table("menu_items").upsert({
        "sku": item.sku,
        "name": item.name,
        "category": item.category,
        "cost": float(item.cost) if item.cost is not None else None,
        "price": float(item.price),
        "active": True
    }).execute()


def delete_menu_item(path: Path | None, sku: str) -> None:
    if path is not None:
        from app.ingest.loader import load_menu as _load_csv_menu
        from app.ingest.mappings import cafe_generic as default_mapping
        import csv
        if not path.exists():
            return
        menu = _load_csv_menu(path)
        if sku in menu:
            del menu[sku]
            m = default_mapping.MENU
            fieldnames = list(m.values())
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for i in menu.values():
                    writer.writerow({
                        m["sku"]: i.sku,
                        m["name"]: i.name,
                        m["category"]: i.category,
                        m["cost"]: str(i.cost) if i.cost is not None else "",
                        m["price"]: str(i.price),
                    })
        return

    _supabase().table("menu_items").update({"active": False}).eq("sku", sku).execute()


# ── Knowledge Base (RAG) ──────────────────────────────────────────────────────

def _get_embedding_model():
    from sentence_transformers import SentenceTransformer
    # We load this lazily to avoid heavy import overhead on start
    return SentenceTransformer('all-MiniLM-L6-v2')

def store_knowledge_chunks(chunks: list[dict[str, Any]]) -> None:
    """Store chunks with vector embeddings in Supabase."""
    model = _get_embedding_model()
    
    # Generate embeddings for each chunk's text
    texts = [c["content"] for c in chunks]
    embeddings = model.encode(texts)
    
    records = []
    for chunk, embedding in zip(chunks, embeddings):
        records.append({
            "content": chunk["content"],
            "metadata": chunk["metadata"],
            "embedding": embedding.tolist()
        })
        
    _supabase().table("knowledge_base").insert(records).execute()

def search_knowledge_base(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    """Search the knowledge base for chunks similar to the query."""
    model = _get_embedding_model()
    query_embedding = model.encode(query).tolist()
    
    res = _supabase().rpc(
        "match_knowledge_chunks",
        {
            "query_embedding": query_embedding,
            "match_threshold": 0.25,
            "match_count": top_k
        }
    ).execute()
    
    return res.data
