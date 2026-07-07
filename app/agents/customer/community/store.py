"""Multi-store PostgreSQL persistence layer for the customer agent.

Every public function takes store_id as the first argument to scope all
queries to the correct restaurant. Raw data isolation between stores is
enforced at the query level — no cross-store leakage is possible.

All access goes through the shared DATABASE_URL via SQLAlchemy.
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
import app.core.cache as _cache


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
    cached = _cache.get(f"vc:{store_id}")
    if cached:
        return VenueConfig(**cached)
    try:
        with SessionLocal() as db:
            row = db.query(OrmVenueConfig).filter(OrmVenueConfig.store_id == store_id).first()
            if row is None:
                return VenueConfig(venue_name=_store_name_fallback(store_id))
            cfg = VenueConfig(
                venue_name=row.venue_name or "Restaurant",
                stamp_goal=row.stamp_goal or 5,
                reward_text=row.reward_text or "a free drink or dessert",
                winback_days=row.winback_days or 10,
                code_expiry_days=row.code_expiry_days or 30,
                owner_phones=list(row.owner_phones or []),
                qr_greeting=row.qr_greeting or "",
            )
    except Exception:
        return VenueConfig(venue_name=_store_name_fallback(store_id))
    try:
        _cache.set(f"vc:{store_id}", cfg.model_dump(), ttl=300)
    except Exception:
        pass
    return cfg


# ── CommunityMember ───────────────────────────────────────────────────────────

def load_members(store_id: int) -> dict[str, CommunityMember]:
    cached = _cache.get(f"mem:{store_id}")
    if cached:
        return {phone: CommunityMember(**data) for phone, data in cached.items()}
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
    except Exception:
        return {}
    try:
        _cache.set(f"mem:{store_id}", {p: m.model_dump() for p, m in members.items()}, ttl=30)
    except Exception:
        pass
    return members


def save_member(store_id: int, member: CommunityMember) -> None:
    """Upsert ONE member. Prefer this in request handlers: saving the whole
    load_members() snapshot re-writes every member with possibly-stale values,
    and two concurrent requests clobber each other's changes (proven live —
    concurrent onboardings lost registrations)."""
    save_members(store_id, {member.phone: member})


def save_members(store_id: int, members: dict[str, CommunityMember]) -> None:
    """Upsert the given members (a subset is fine — rows not included are
    never touched or deleted)."""
    import logging as _log
    from sqlalchemy import text as _text
    try:
        with SessionLocal() as db:
            for m in members.values():
                # Use upsert so a pre-existing phone from any store never causes
                # a silent PK violation (phone is the global primary key).
                db.execute(
                    _text(
                        "INSERT INTO community_members "
                        "  (store_id, phone, name, stamps_current, stamps_lifetime,"
                        "   joined_at, last_activity_at, opted_in, winback_sent_at) "
                        "VALUES"
                        "  (:store_id, :phone, :name, :stamps_current, :stamps_lifetime,"
                        "   :joined_at, :last_activity_at, :opted_in, :winback_sent_at) "
                        "ON CONFLICT (phone) DO UPDATE SET"
                        "  store_id         = EXCLUDED.store_id,"
                        "  name             = EXCLUDED.name,"
                        "  stamps_current   = EXCLUDED.stamps_current,"
                        "  stamps_lifetime  = EXCLUDED.stamps_lifetime,"
                        "  last_activity_at = EXCLUDED.last_activity_at,"
                        "  opted_in         = EXCLUDED.opted_in,"
                        "  winback_sent_at  = EXCLUDED.winback_sent_at"
                    ),
                    {
                        "store_id": store_id,
                        "phone": m.phone,
                        "name": m.name,
                        "stamps_current": m.stamps_current,
                        "stamps_lifetime": m.stamps_lifetime,
                        "joined_at": _parse_dt(m.joined_at),
                        "last_activity_at": _parse_dt(m.last_activity_at),
                        "opted_in": m.opted_in,
                        "winback_sent_at": _parse_dt(m.winback_sent_at),
                    },
                )
            db.commit()
    except Exception as exc:
        _log.getLogger(__name__).warning(
            "save_members: DB write failed for store %d: %s", store_id, exc
        )
        # DB blip: patch the saved members into the cached snapshot (never
        # replace the whole snapshot — that erases members added by
        # concurrent requests) so in-session state survives until retry.
        try:
            cached = _cache.get(f"mem:{store_id}") or {}
            cached.update({p: m.model_dump() for p, m in members.items()})
            _cache.set(f"mem:{store_id}", cached, ttl=30)
        except Exception:
            pass
        return
    # Invalidate rather than overwrite: writing this caller's snapshot back
    # would erase members registered by concurrent requests for up to the
    # cache TTL (the exact race the 5-user stress test exposed). The next
    # load repopulates from the DB, which is the source of truth.
    _cache.delete(f"mem:{store_id}")


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
    cached = _cache.get(f"ob:{store_id}")
    if cached is not None:
        return cached
    try:
        with SessionLocal() as db:
            rows = db.query(OrmOnboardingSession).filter(OrmOnboardingSession.store_id == store_id).all()
            sessions = {r.phone: r.state for r in rows}
            _cache.set(f"ob:{store_id}", sessions, ttl=60)
            return sessions
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
        _cache.delete(f"ob:{store_id}")
    except Exception:
        _cache.delete(f"ob:{store_id}")


def clear_onboarding_session(store_id: int, phone: str) -> None:
    try:
        with SessionLocal() as db:
            db.query(OrmOnboardingSession).filter(
                OrmOnboardingSession.store_id == store_id,
                OrmOnboardingSession.phone == phone,
            ).delete(synchronize_session=False)
            db.commit()
        _cache.delete(f"ob:{store_id}")
    except Exception:
        _cache.delete(f"ob:{store_id}")


# ── Chat Sessions ─────────────────────────────────────────────────────────────

def load_chat_session(store_id: int, phone: str) -> list[dict]:
    cached = _cache.get(f"chat:{store_id}:{phone}")
    if cached is not None:
        return cached
    try:
        with SessionLocal() as db:
            row = db.query(OrmChatSession).filter(
                OrmChatSession.store_id == store_id,
                OrmChatSession.phone == phone,
            ).first()
            history = list(row.history or []) if row else []
            _cache.set(f"chat:{store_id}:{phone}", history, ttl=3600)
            return history
    except Exception:
        return []


def save_chat_session(store_id: int, phone: str, history: list[dict]) -> None:
    # Redis is primary — fast write for the hot path
    _cache.set(f"chat:{store_id}:{phone}", history, ttl=3600)
    # DB write for durability (non-blocking from caller's perspective)
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

_EMBEDDING_MODEL = None

def _embedding_model():
    """Return a cached SentenceTransformer model, or None if unavailable."""
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is not None:
        return _EMBEDDING_MODEL
    try:
        from sentence_transformers import SentenceTransformer
        _EMBEDDING_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        return _EMBEDDING_MODEL
    except Exception:
        return None


def search_knowledge_base(store_id: int, query: str, top_k: int = 3) -> list[dict]:
    from sqlalchemy import text

    # Try vector search first
    model = _embedding_model()
    if model is not None:
        try:
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
                rows = [dict(r._mapping) for r in result]
            if rows:
                return rows
        except Exception:
            pass

    # Fallback: keyword text search (works without sentence_transformers)
    try:
        keywords = [w for w in query.lower().split() if len(w) > 2]
        if not keywords:
            return []
        with SessionLocal() as db:
            like_clauses = " OR ".join(f"LOWER(content) LIKE :kw{i}" for i in range(len(keywords)))
            params: dict = {"store_id": store_id, "limit": top_k}
            params.update({f"kw{i}": f"%{kw}%" for i, kw in enumerate(keywords)})
            result = db.execute(
                text(
                    f"SELECT id, store_id, content, metadata, 0.5 AS similarity "
                    f"FROM knowledge_base "
                    f"WHERE store_id = :store_id AND ({like_clauses}) "
                    f"LIMIT :limit"
                ),
                params,
            )
            return [dict(r._mapping) for r in result]
    except Exception:
        return []


def store_knowledge_chunks(store_id: int, documents: list[dict]) -> None:
    from sqlalchemy import text

    model = _embedding_model()
    with SessionLocal() as db:
        for doc in documents:
            try:
                if model is not None:
                    embedding = model.encode(doc["content"]).tolist()
                    db.execute(
                        text(
                            "INSERT INTO knowledge_base (store_id, content, embedding, metadata) "
                            "VALUES (:store_id, :content, CAST(:embedding AS vector), CAST(:metadata AS jsonb)) "
                            "ON CONFLICT DO NOTHING"
                        ),
                        {
                            "store_id": store_id,
                            "content": doc["content"],
                            "embedding": _json.dumps(embedding),
                            "metadata": _json.dumps(doc.get("metadata", {})),
                        },
                    )
                else:
                    # No ML available — store without embedding, text search will still work
                    db.execute(
                        text(
                            "INSERT INTO knowledge_base (store_id, content, metadata) "
                            "VALUES (:store_id, :content, CAST(:metadata AS jsonb))"
                        ),
                        {
                            "store_id": store_id,
                            "content": doc["content"],
                            "metadata": _json.dumps(doc.get("metadata", {})),
                        },
                    )
            except Exception:
                pass
        db.commit()


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
