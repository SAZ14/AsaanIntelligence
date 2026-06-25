from __future__ import annotations
import logging
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, DateTime,
    Text, ForeignKey, JSON, UniqueConstraint, Boolean,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from app.config import DATABASE_URL

logger = logging.getLogger(__name__)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ---------------------------------------------------------------------------
# Shared tables (same as scout agent — single source of truth in Supabase)
# ---------------------------------------------------------------------------

class Store(Base):
    __tablename__ = "stores"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    location = Column(String, nullable=True)
    category = Column(String, nullable=True)
    instagram_handle = Column(String, nullable=True)
    config = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)


class StoreLocation(Base):
    __tablename__ = "store_locations"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    address = Column(String, nullable=False)
    city = Column(String, nullable=True)
    area = Column(String, nullable=True)
    is_primary = Column(String, default="false")
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
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Integrity-agent-specific tables
# ---------------------------------------------------------------------------

class POSConnection(Base):
    """How to reach a store's POS system. Raw transaction data NEVER stored here."""
    __tablename__ = "pos_connections"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, unique=True)
    pos_type = Column(String, default="csv")        # "csv" | "rest"
    config = Column(JSON, default=dict)             # {"base_dir": "..."} or {"base_url": "...", "api_key": "..."}
    mapping = Column(String, default="cafe_generic")
    currency = Column(String, default="PKR")
    timezone = Column(String, default="Asia/Karachi")
    created_at = Column(DateTime, default=datetime.utcnow)


class IntegrityRun(Base):
    """High-level stats from one integrity audit run. No raw POS transactions stored."""
    __tablename__ = "integrity_runs"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String, default="ok")           # "ok" | "error"
    period_days = Column(Integer, nullable=True)
    net_sales = Column(Float, nullable=True)
    gross_profit = Column(Float, nullable=True)
    gross_margin = Column(Float, nullable=True)
    estimated_leakage = Column(Float, nullable=True)
    finding_count = Column(Integer, default=0)
    llm_used = Column(String, default="false")


class IntegrityReport(Base):
    """Stores the formatted audit report text (executive summary + command output)."""
    __tablename__ = "integrity_reports"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    run_id = Column(Integer, ForeignKey("integrity_runs.id"), nullable=False)
    command = Column(String, nullable=False)        # "summary" | "leakage" | "profit" | etc.
    report_text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Session / store lookup helpers
# ---------------------------------------------------------------------------

def get_stores_for_number(whatsapp: str) -> list[Store]:
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


def get_pos_connection(store_id: int) -> POSConnection | None:
    with SessionLocal() as db:
        pos = db.query(POSConnection).filter(POSConnection.store_id == store_id).first()
        if pos:
            db.expunge(pos)
        return pos


def save_integrity_run(
    store_id: int,
    period_days: int,
    net_sales: float,
    gross_profit: float,
    gross_margin: float,
    estimated_leakage: float,
    finding_count: int,
    llm_used: bool,
) -> int:
    with SessionLocal() as db:
        run = IntegrityRun(
            store_id=store_id,
            finished_at=datetime.utcnow(),
            status="ok",
            period_days=period_days,
            net_sales=net_sales,
            gross_profit=gross_profit,
            gross_margin=gross_margin,
            estimated_leakage=estimated_leakage,
            finding_count=finding_count,
            llm_used=str(llm_used).lower(),
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run.id


def save_integrity_report(store_id: int, run_id: int, command: str, report_text: str) -> None:
    with SessionLocal() as db:
        db.add(IntegrityReport(
            store_id=store_id,
            run_id=run_id,
            command=command,
            report_text=report_text,
        ))
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
