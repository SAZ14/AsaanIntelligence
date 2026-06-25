from __future__ import annotations
import logging
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, DateTime,
    Text, ForeignKey, JSON, UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from app.config import DATABASE_URL  # noqa: E402  (COMPETITORS imported locally where needed)

logger = logging.getLogger(__name__)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Store(Base):
    __tablename__ = "stores"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    location = Column(String, nullable=True)
    category = Column(String, nullable=True)
    instagram_handle = Column(String, nullable=True)
    config = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)


class StoreMember(Base):
    """Maps WhatsApp numbers to stores. One number can belong to many stores."""
    __tablename__ = "store_members"
    __table_args__ = (UniqueConstraint("store_id", "whatsapp"),)

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    whatsapp = Column(String, nullable=False)
    role = Column(String, default="owner")
    created_at = Column(DateTime, default=datetime.utcnow)


class UserSession(Base):
    """Tracks which store a WhatsApp number is currently chatting about."""
    __tablename__ = "user_sessions"

    whatsapp = Column(String, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=True)  # NULL = awaiting selection
    updated_at = Column(DateTime, default=datetime.utcnow)


class Competitor(Base):
    __tablename__ = "competitors"
    __table_args__ = (UniqueConstraint("store_id", "name"),)

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    name = Column(String, nullable=False)
    category = Column(String, nullable=True)
    instagram_handle = Column(String, nullable=True)
    website = Column(String, nullable=True)
    place_id = Column(String, nullable=True)
    source = Column(String, default="seed")
    created_at = Column(DateTime, default=datetime.utcnow)


class Run(Base):
    __tablename__ = "runs"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    command = Column(String, nullable=False)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String, default="running")
    sources_ok = Column(JSON, default=list)
    sources_failed = Column(JSON, default=list)
    finding_count = Column(Integer, default=0)


class Finding(Base):
    __tablename__ = "findings"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False)
    competitor_name = Column(String, nullable=False)
    source_platform = Column(String, nullable=False)
    update_type = Column(String, nullable=False)
    content_text = Column(Text, nullable=False)
    rating = Column(Float, nullable=True)
    post_date = Column(DateTime, nullable=True)
    source_url = Column(String, nullable=True)
    image_url = Column(String, nullable=True)
    engagement = Column(JSON, nullable=True)
    collected_at = Column(DateTime, default=datetime.utcnow)
    ai_summary = Column(Text, nullable=True)
    relevance_score = Column(Integer, nullable=True)
    content_hash = Column(String, nullable=False, index=True)


class Report(Base):
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False)
    command = Column(String, nullable=False)
    report_text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Session / store lookup helpers (used by main.py webhook routing)
# ---------------------------------------------------------------------------

def get_stores_for_number(whatsapp: str) -> list[Store]:
    """Return all stores this WhatsApp number has access to."""
    with SessionLocal() as db:
        members = db.query(StoreMember).filter(StoreMember.whatsapp == whatsapp).all()
        store_ids = [m.store_id for m in members]
        if not store_ids:
            return []
        stores = db.query(Store).filter(Store.id.in_(store_ids)).all()
        db.expunge_all()
        return stores


def get_user_session(whatsapp: str) -> UserSession | None:
    with SessionLocal() as db:
        s = db.query(UserSession).filter(UserSession.whatsapp == whatsapp).first()
        if s:
            db.expunge(s)
        return s


def set_user_session(whatsapp: str, store_id: int | None) -> None:
    with SessionLocal() as db:
        s = db.query(UserSession).filter(UserSession.whatsapp == whatsapp).first()
        if s:
            s.store_id = store_id
            s.updated_at = datetime.utcnow()
        else:
            db.add(UserSession(whatsapp=whatsapp, store_id=store_id))
        db.commit()


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created/verified")


def _seed_competitors(store_id: int, db: Session, competitors: list) -> None:
    for c in competitors:
        db.add(Competitor(
            store_id=store_id,
            name=c["name"],
            category=c.get("category"),
            instagram_handle=c.get("instagram_handle"),
            website=c.get("website"),
            source="seed",
        ))
    db.commit()
    logger.info("Seeded %d competitors for store_id=%d", len(competitors), store_id)
