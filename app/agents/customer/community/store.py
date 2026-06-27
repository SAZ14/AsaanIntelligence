"""Multi-store PostgreSQL persistence layer for the customer agent.

Every public function takes store_id as the first argument to scope all
queries to the correct restaurant. Raw data isolation between stores is
enforced at the query level — no cross-store leakage is possible.

All access goes through the shared DATABASE_URL via SQLAlchemy; the
supabase-py client is not used anywhere in this module.
"""
from __future__ import annotations

import json as _json
from datetime import datetime

from app.core.db import (
    SessionLocal,
    CommunityMember as OrmMember,
    CustomerChatSession as OrmChatSession,
    Deal as OrmDeal,
    OnboardingSession as OrmOnboardingSession,
    RedeemCode as OrmRedeemCode,
    StampEvent as OrmStampEvent,
    Store as OrmStore,
    VenueConfig as OrmVenueConfig,
)
from app.agents.customer.community.models import (
    CommunityMember, Deal, RedeemCode, StampEvent, VenueConfig,
)


def _parse_dt(s) -> datetime | None:
    if s is None or s == "":
        return None
    if isinstance(s, datetime):
        return s
    try:
        return datetime.fromisoformat(str(s))
    except Exception:
        return None


def _fmt_dt(dt) -> str:
    if dt is None:
        return ""
    return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)


# ── VenueConfig ──────────────────────────────────────────────────────────────

def _store_name_fallback(store_id: int) -> str:
    try:
        with SessionLocal() as db:
            s = db.query(OrmStore).filter(OrmStore.id == store_id).first()
            return s.name if s else "Restaurant"
    except Exception:
        return "Restaurant"


def load_venue_config(store_id: int) -> VenueConfig:
    try:
        with SessionLocal() as db:
            row = db.query(OrmVenueConfig).filter(OrmVenueConfig.store_id == store_id).first()
            if row is None:
                return VenueConfig(venue_name=_store_name_fallback(store_id))
            return VenueConfig(
                venue_name=row.venue_name or "Restaurant",
                stamp_goal=row.stamp_goal or 5,
                reward_text=row.reward_text or "a free drink or dessert",
                winback_days=row.winback_days or 5,
                code_expiry_days=row.code_expiry_days or 30,
                owner_phones=list(row.owner_phones or []),
                qr_greeting=row.qr_greeting or "",
            )
    except Exception:
        return VenueConfig(venue_name=_store_name_fallback(store_id))


# ── CommunityMember ───────────────────────────────────────────────────────────

def load_members(store_id: int) -> dict[str, CommunityMember]:
    try:
        with SessionLocal() as db:
            rows = db.query(OrmMember).filter(OrmMember.store_id == store_id).all()
            members = {}
            for row in rows:
                m = CommunityMember(
                    phone=row.phone,
                    name=row.name or "",
                    stamps_current=row.stamps_current or 0,
                    stamps_lifetime=row.stamps_lifetime or 0,
                    joined_at=_fmt_dt(row.joined_at),
                    last_activity_at=_fmt_dt(row.last_activity_at),
                    opted_in=row.opted_in if row.opted_in is not None else True,
                    winback_sent_at=_fmt_dt(row.winback_sent_at),
                )
                members[m.phone] = m
            return members
    except Exception:
        return {}


def save_members(store_id: int, members: dict[str, CommunityMember]) -> None:
    try:
        with SessionLocal() as db:
            for m in members.values():
                row = db.query(OrmMember).filter(
                    OrmMember.store_id == store_id,
                    OrmMember.phone == m.phone,
                ).first()
                if row is None:
                    row = OrmMember(store_id=store_id, phone=m.phone)
                    db.add(row)
                row.name = m.name
                row.stamps_current = m.stamps_current
                row.stamps_lifetime = m.stamps_lifetime
                row.joined_at = _parse_dt(m.joined_at)
                row.last_activity_at = _parse_dt(m.last_activity_at)
                row.opted_in = m.opted_in
                row.winback_sent_at = _parse_dt(m.winback_sent_at)
            db.commit()
    except Exception:
        pass


# ── RedeemCode ────────────────────────────────────────────────────────────────

def load_redeem_codes(store_id: int) -> list[RedeemCode]:
    try:
        with SessionLocal() as db:
            rows = (
                db.query(OrmRedeemCode)
                .filter(OrmRedeemCode.store_id == store_id)
                .order_by(OrmRedeemCode.issued_at)
                .all()
            )
            return [
                RedeemCode(
                    code=r.code,
                    order_id=r.order_id or "",
                    issued_at=_fmt_dt(r.issued_at),
                    redeemed_at=_fmt_dt(r.redeemed_at),
                    redeemed_by=r.redeemed_by or "",
                )
                for r in rows
            ]
    except Exception:
        return []


def append_redeem_code(store_id: int, code: RedeemCode) -> None:
    try:
        with SessionLocal() as db:
            db.add(OrmRedeemCode(
                store_id=store_id,
                code=code.code,
                order_id=code.order_id or None,
                issued_at=_parse_dt(code.issued_at) or datetime.utcnow(),
            ))
            db.commit()
    except Exception:
        pass


def update_redeem_code(store_id: int, code: RedeemCode) -> None:
    try:
        with SessionLocal() as db:
            row = db.query(OrmRedeemCode).filter(
                OrmRedeemCode.store_id == store_id,
                OrmRedeemCode.code == code.code,
            ).first()
            if row:
                row.redeemed_at = _parse_dt(code.redeemed_at)
                row.redeemed_by = code.redeemed_by or None
                db.commit()
    except Exception:
        pass


# ── StampEvent ────────────────────────────────────────────────────────────────

def load_stamp_events(store_id: int) -> list[StampEvent]:
    try:
        with SessionLocal() as db:
            rows = (
                db.query(OrmStampEvent)
                .filter(OrmStampEvent.store_id == store_id)
                .order_by(OrmStampEvent.at)
                .all()
            )
            return [
                StampEvent(
                    phone=r.phone,
                    code=r.code,
                    stamp_number=r.stamp_number,
                    reward_issued=r.reward_issued or False,
                    at=_fmt_dt(r.at),
                )
                for r in rows
            ]
    except Exception:
        return []


def append_stamp_event(store_id: int, event: StampEvent) -> None:
    try:
        with SessionLocal() as db:
            db.add(OrmStampEvent(
                store_id=store_id,
                phone=event.phone,
                code=event.code,
                stamp_number=event.stamp_number,
                reward_issued=event.reward_issued,
                at=_parse_dt(event.at) or datetime.utcnow(),
            ))
            db.commit()
    except Exception:
        pass


# ── Deals ─────────────────────────────────────────────────────────────────────

def load_deals(store_id: int) -> list[Deal]:
    try:
        with SessionLocal() as db:
            rows = db.query(OrmDeal).filter(OrmDeal.store_id == store_id).all()
            return [Deal(title=r.title, description=r.description or "", active=r.active) for r in rows]
    except Exception:
        return []


# ── Onboarding Sessions ───────────────────────────────────────────────────────

def load_onboarding_sessions(store_id: int) -> dict[str, str]:
    try:
        with SessionLocal() as db:
            rows = db.query(OrmOnboardingSession).filter(OrmOnboardingSession.store_id == store_id).all()
            return {r.phone: r.state for r in rows}
    except Exception:
        return {}


def save_onboarding_sessions(store_id: int, sessions: dict[str, str]) -> None:
    if not sessions:
        return
    try:
        with SessionLocal() as db:
            for phone, state in sessions.items():
                row = db.query(OrmOnboardingSession).filter(
                    OrmOnboardingSession.store_id == store_id,
                    OrmOnboardingSession.phone == phone,
                ).first()
                if row is None:
                    db.add(OrmOnboardingSession(store_id=store_id, phone=phone, state=state))
                else:
                    row.state = state
            db.commit()
    except Exception:
        pass


def clear_onboarding_session(store_id: int, phone: str) -> None:
    try:
        with SessionLocal() as db:
            db.query(OrmOnboardingSession).filter(
                OrmOnboardingSession.store_id == store_id,
                OrmOnboardingSession.phone == phone,
            ).delete(synchronize_session=False)
            db.commit()
    except Exception:
        pass


# ── Chat Sessions ─────────────────────────────────────────────────────────────

def load_chat_session(store_id: int, phone: str) -> list[dict]:
    try:
        with SessionLocal() as db:
            row = db.query(OrmChatSession).filter(
                OrmChatSession.store_id == store_id,
                OrmChatSession.phone == phone,
            ).first()
            if row is None:
                return []
            return list(row.history or [])
    except Exception:
        return []


def save_chat_session(store_id: int, phone: str, history: list[dict]) -> None:
    try:
        with SessionLocal() as db:
            row = db.query(OrmChatSession).filter(
                OrmChatSession.store_id == store_id,
                OrmChatSession.phone == phone,
            ).first()
            if row is None:
                db.add(OrmChatSession(store_id=store_id, phone=phone, history=history))
            else:
                row.history = history
                row.updated_at = datetime.utcnow()
            db.commit()
    except Exception:
        pass


# ── Knowledge Base (RAG via pgvector) ────────────────────────────────────────

def search_knowledge_base(store_id: int, query: str, top_k: int = 3) -> list[dict]:
    try:
        from sentence_transformers import SentenceTransformer
        from sqlalchemy import text
        model = SentenceTransformer("all-MiniLM-L6-v2")
        embedding = model.encode(query).tolist()
        with SessionLocal() as db:
            result = db.execute(
                text(
                    "SELECT * FROM match_knowledge_chunks("
                    "  query_embedding := CAST(:embedding AS vector),"
                    "  match_threshold := :threshold,"
                    "  match_count := :count,"
                    "  store_id_filter := :store_id"
                    ")"
                ),
                {
                    "embedding": _json.dumps(embedding),
                    "threshold": 0.25,
                    "count": top_k,
                    "store_id": store_id,
                },
            )
            return [dict(r._mapping) for r in result]
    except Exception:
        return []


def store_knowledge_chunks(store_id: int, documents: list[dict]) -> None:
    try:
        from sentence_transformers import SentenceTransformer
        from sqlalchemy import text
        model = SentenceTransformer("all-MiniLM-L6-v2")
        with SessionLocal() as db:
            for doc in documents:
                embedding = model.encode(doc["content"]).tolist()
                db.execute(
                    text(
                        "INSERT INTO knowledge_base (store_id, content, embedding, metadata) "
                        "VALUES (:store_id, :content, CAST(:embedding AS vector), CAST(:metadata AS jsonb))"
                    ),
                    {
                        "store_id": store_id,
                        "content": doc["content"],
                        "embedding": _json.dumps(embedding),
                        "metadata": _json.dumps(doc.get("metadata", {})),
                    },
                )
            db.commit()
    except Exception:
        pass


def clear_knowledge_by_source(store_id: int, source: str) -> None:
    try:
        from sqlalchemy import text
        with SessionLocal() as db:
            db.execute(
                text(
                    "DELETE FROM knowledge_base "
                    "WHERE store_id = :store_id AND metadata->>'source' = :source"
                ),
                {"store_id": store_id, "source": source},
            )
            db.commit()
    except Exception:
        pass
