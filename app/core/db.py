"""Unified SQLAlchemy models for the central server.

All agents share this schema. Tables are partitioned by store_id so a single
Postgres instance serves every restaurant / chain.

Chains:  one brand (e.g. "Sugar Rush") can have many stores (locations).
         chain_id groups them; the reputation agent queries across locations.
Stores:  one entry per physical location, with optional chain_id.
Members: staff / owners — mapped to stores via store_members.
         The store_members.role column gates internal-agent access.
Customers use separate community_* tables with store_id partitioning.
"""
from __future__ import annotations
import logging
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer,
    JSON, String, Text, UniqueConstraint, create_engine,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from app.core.config import DATABASE_URL

logger = logging.getLogger(__name__)

_kw = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
engine = create_engine(
    DATABASE_URL,
    connect_args=_kw,
    pool_pre_ping=True,   # test connection before use; reconnects if the DB dropped it
    pool_recycle=300,     # recycle connections every 5 min to avoid SSL EOF on idle
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ---------------------------------------------------------------------------
# Chain / Store hierarchy
# ---------------------------------------------------------------------------

class Chain(Base):
    """Brand / restaurant group. Many stores (locations) belong to one chain."""
    __tablename__ = "chains"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Store(Base):
    """One physical location. chain_id links sibling locations."""
    __tablename__ = "stores"

    id = Column(Integer, primary_key=True)
    chain_id = Column(Integer, ForeignKey("chains.id"), nullable=True)
    name = Column(String, nullable=False)
    location = Column(String, nullable=True)
    category = Column(String, nullable=True)
    instagram_handle = Column(String, nullable=True)
    config = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)


class StoreLocation(Base):
    """Address details for a store (separate from the store name/location string)."""
    __tablename__ = "store_locations"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    address = Column(String, nullable=False)
    city = Column(String, nullable=True)
    area = Column(String, nullable=True)
    is_primary = Column(String, default="false")
    created_at = Column(DateTime, default=datetime.utcnow)


class StoreMember(Base):
    """Maps WhatsApp numbers to stores for internal-agent access.
    Role values: owner | manager | staff
    Any number in this table is whitelisted for the internal channel.
    """
    __tablename__ = "store_members"
    __table_args__ = (UniqueConstraint("store_id", "whatsapp"),)

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    whatsapp = Column(String, nullable=False)
    role = Column(String, default="owner")
    created_at = Column(DateTime, default=datetime.utcnow)


class UserSession(Base):
    """Tracks which store an internal user is currently querying."""
    __tablename__ = "user_sessions"

    whatsapp = Column(String, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=True)
    active_agent = Column(String, nullable=True)   # "scout" | "integrity" | "revenue"
    updated_at = Column(DateTime, default=datetime.utcnow)


class StoreTwilioNumber(Base):
    """Customer-facing Twilio WhatsApp number, one per store.
    When a customer texts this number, they reach that store's customer agent.
    QR codes link to this number.
    """
    __tablename__ = "store_twilio_numbers"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, unique=True)
    whatsapp_number = Column(String, nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class StoreOpenWASession(Base):
    """OpenWA session for a store — used while Twilio business verification is pending.
    One session per store WhatsApp number. session_id matches the ID in the OpenWA
    instance; phone_number is the E.164 number scanned into that session.
    """
    __tablename__ = "store_openwa_sessions"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, unique=True)
    session_id = Column(String, nullable=False, unique=True)
    phone_number = Column(String, nullable=False)   # E.164: "+923XXXXXXXXX"
    created_at = Column(DateTime, default=datetime.utcnow)


class StoreMetaNumber(Base):
    """WhatsApp Business Platform (Meta Cloud API) number for a store.

    Populated when a restaurant onboards their WhatsApp Business number to
    our Meta app via embedded signup: the flow yields a WABA id, a phone
    number id, and a business access token scoped to that WABA. Inbound
    webhooks are matched to the store by phone_number_id; outbound sends
    use that store's own access token."""
    __tablename__ = "store_meta_numbers"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, unique=True)
    phone_number_id = Column(String, nullable=False, unique=True)
    waba_id = Column(String, nullable=False, default="")
    access_token = Column(Text, nullable=False)
    display_number = Column(String, nullable=False, default="")  # E.164 for humans
    created_at = Column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Scout agent tables
# ---------------------------------------------------------------------------

class Competitor(Base):
    __tablename__ = "competitors"
    __table_args__ = (UniqueConstraint("store_id", "name"),)

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    name = Column(String, nullable=False)
    category = Column(String, nullable=True)
    city = Column(String, nullable=True)
    instagram_handle = Column(String, nullable=True)
    website = Column(String, nullable=True)
    place_id = Column(String, nullable=True)
    source = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ScoutRun(Base):
    __tablename__ = "runs"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    command = Column(String, nullable=False)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String, default="ok")
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
    engagement = Column(JSON, default=dict)
    collected_at = Column(DateTime, default=datetime.utcnow)
    ai_summary = Column(Text, nullable=True)
    relevance_score = Column(Integer, nullable=True)
    content_hash = Column(String, nullable=False)


class ScoutReport(Base):
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False)
    command = Column(String, nullable=False)
    report_text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Integrity agent tables
# ---------------------------------------------------------------------------

class POSConnection(Base):
    """POS system config for a store. Raw transaction data NEVER stored here."""
    __tablename__ = "pos_connections"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, unique=True)
    pos_type = Column(String, default="csv")
    config = Column(JSON, default=dict)
    mapping = Column(String, default="cafe_generic")
    currency = Column(String, default="PKR")
    timezone = Column(String, default="Asia/Karachi")
    created_at = Column(DateTime, default=datetime.utcnow)


class POSColumnMapping(Base):
    """Learned column mapping for a POS export format.

    When staff upload a CSV whose headers don't match the canonical schema,
    the mapping is inferred once (LLM-assisted), validated against the actual
    file, and stored here keyed by a fingerprint of the header row. Every
    later upload with the same headers is translated deterministically —
    no LLM involved. One store can accumulate multiple formats (POS change,
    different report screens)."""
    __tablename__ = "pos_column_mappings"
    __table_args__ = (UniqueConstraint("store_id", "file_type", "header_fingerprint"),)

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    file_type = Column(String, nullable=False)          # "pos_sales" | "pos_menu" | "pos_staff"
    header_fingerprint = Column(String, nullable=False)  # sha1 of normalized headers
    column_map = Column(JSON, nullable=False)            # canonical field -> source column (or [date_col, time_col])
    source = Column(String, nullable=False, default="llm")  # "llm" | "identity" | "manual"
    created_at = Column(DateTime, default=datetime.utcnow)


class UploadedFile(Base):
    """CSV files uploaded via WhatsApp by whitelisted staff.
    One row per (store, file_type). Re-uploading the same type replaces it.
    Raw POS transaction data is stored here temporarily for analysis only."""
    __tablename__ = "uploaded_files"
    __table_args__ = (UniqueConstraint("store_id", "file_type"),)

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    file_type = Column(String, nullable=False)   # "pos_sales" | "pos_menu" | "pos_staff"
    filename = Column(String, nullable=True)
    content = Column(Text, nullable=False)
    uploaded_by = Column(String, nullable=True)  # whatsapp number of uploader
    uploaded_at = Column(DateTime, default=datetime.utcnow)


class IntegrityRun(Base):
    """High-level stats from one audit run. No raw POS data stored."""
    __tablename__ = "integrity_runs"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String, default="ok")
    period_days = Column(Integer, nullable=True)
    net_sales = Column(Float, nullable=True)
    gross_profit = Column(Float, nullable=True)
    gross_margin = Column(Float, nullable=True)
    estimated_leakage = Column(Float, nullable=True)
    finding_count = Column(Integer, default=0)
    llm_used = Column(String, default="false")


class IntegrityReport(Base):
    __tablename__ = "integrity_reports"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    run_id = Column(Integer, ForeignKey("integrity_runs.id"), nullable=False)
    command = Column(String, nullable=False)
    report_text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Revenue agent tables
# ---------------------------------------------------------------------------

class RevenueConnection(Base):
    """Revenue agent data-source config per store."""
    __tablename__ = "revenue_connections"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, unique=True)
    data_dir = Column(String, nullable=True)
    db_path = Column(String, nullable=True)
    config = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Reputation agent tables
# ---------------------------------------------------------------------------

class ReputationConfig(Base):
    """Per-store reputation agent configuration — scrape targets + brand voice."""
    __tablename__ = "reputation_configs"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, unique=True)
    google_maps_terms = Column(JSON, default=list)       # ["Venue Name", "Venue Name City"]
    google_maps_location = Column(String, nullable=True) # "City, Country"
    foodpanda_url = Column(String, nullable=True)
    foodpanda_keyword = Column(String, nullable=True)
    instagram_usernames = Column(JSON, default=list)     # ["handle1", "handle2"]
    brand_voice_tone = Column(String, nullable=True)     # "Warm, genuine, professional"
    brand_voice_never_say = Column(JSON, default=list)   # ["unfortunately", "sorry for"]
    updated_at = Column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Customer agent tables (store-scoped versions of community tables)
# ---------------------------------------------------------------------------

class VenueConfig(Base):
    """Per-store customer-agent configuration."""
    __tablename__ = "venue_config"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=True, unique=True)
    venue_name = Column(String, nullable=False)
    stamp_goal = Column(Integer, nullable=False, default=5)
    reward_text = Column(String, nullable=False, default="a free drink or dessert")
    winback_days = Column(Integer, nullable=False, default=10)
    code_expiry_days = Column(Integer, nullable=False, default=30)
    # ARRAY on Postgres (prod schema unchanged); JSON variant so the SQLite
    # test DB can compile this table -- without it create_all() fails and
    # every DB-touching test in the suite errors out.
    owner_phones = Column(
        ARRAY(String).with_variant(JSON(), "sqlite"), nullable=False, default=list
    )
    qr_greeting = Column(String, nullable=False, default="")
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class CommunityMember(Base):
    __tablename__ = "community_members"
    __table_args__ = (UniqueConstraint("store_id", "phone"),)

    phone = Column(String, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=True)
    name = Column(String, nullable=False, default="")
    stamps_current = Column(Integer, nullable=False, default=0)
    stamps_lifetime = Column(Integer, nullable=False, default=0)
    joined_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_activity_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    opted_in = Column(Boolean, nullable=False, default=True)
    winback_sent_at = Column(DateTime, nullable=True)


class RedeemCode(Base):
    __tablename__ = "redeem_codes"

    code = Column(String, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=True)
    order_id = Column(String, nullable=False, default="")
    issued_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    redeemed_at = Column(DateTime, nullable=True)
    redeemed_by = Column(String, nullable=True)


class StampEvent(Base):
    __tablename__ = "stamp_events"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=True)
    phone = Column(String, nullable=False)
    code = Column(String, nullable=False)
    stamp_number = Column(Integer, nullable=False)
    reward_issued = Column(Boolean, nullable=False, default=False)
    at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Deal(Base):
    __tablename__ = "deals"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=True)
    title = Column(String, nullable=False)
    description = Column(String, nullable=False)
    active = Column(Boolean, nullable=False, default=True)


class OnboardingSession(Base):
    __tablename__ = "onboarding_sessions"
    __table_args__ = (UniqueConstraint("store_id", "phone"),)

    phone = Column(String, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=True)
    state = Column(String, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class CustomerChatSession(Base):
    __tablename__ = "chat_sessions"
    __table_args__ = (UniqueConstraint("store_id", "phone"),)

    phone = Column(String, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=True)
    history = Column(JSON, nullable=False, default=list)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Aliases for scout agent pipeline compatibility
Run = ScoutRun
Report = ScoutReport


def is_store_member(whatsapp: str, store_id: int) -> bool:
    """Return True if this WhatsApp number is a whitelisted member of the store."""
    with SessionLocal() as db:
        return db.query(StoreMember).filter(
            StoreMember.store_id == store_id,
            StoreMember.whatsapp == whatsapp,
        ).first() is not None


def get_stores_for_number(whatsapp: str) -> list[Store]:
    with SessionLocal() as db:
        from sqlalchemy import select
        rows = db.query(StoreMember).filter(StoreMember.whatsapp == whatsapp).all()
        ids = [r.store_id for r in rows]
        if not ids:
            return []
        stores = db.query(Store).filter(Store.id.in_(ids)).all()
        db.expunge_all()
        return stores


def get_user_session(whatsapp: str) -> UserSession | None:
    with SessionLocal() as db:
        s = db.query(UserSession).filter(UserSession.whatsapp == whatsapp).first()
        if s:
            db.expunge(s)
        return s


def set_user_session(whatsapp: str, store_id: int | None, active_agent: str | None = None) -> None:
    with SessionLocal() as db:
        s = db.query(UserSession).filter(UserSession.whatsapp == whatsapp).first()
        if s:
            s.store_id = store_id
            s.active_agent = active_agent  # always write — None clears the mode
            s.updated_at = datetime.utcnow()
        else:
            db.add(UserSession(whatsapp=whatsapp, store_id=store_id, active_agent=active_agent))
        db.commit()


def get_store_by_twilio_number(twilio_number: str) -> Store | None:
    """Look up a store by its customer-facing Twilio WhatsApp number."""
    with SessionLocal() as db:
        mapping = db.query(StoreTwilioNumber).filter(
            StoreTwilioNumber.whatsapp_number == twilio_number
        ).first()
        if not mapping:
            return None
        store = db.query(Store).filter(Store.id == mapping.store_id).first()
        if store:
            db.expunge(store)
        return store


def get_store_by_openwa_session(session_id: str) -> Store | None:
    """Look up a store by its OpenWA session ID."""
    with SessionLocal() as db:
        mapping = db.query(StoreOpenWASession).filter(
            StoreOpenWASession.session_id == session_id
        ).first()
        if not mapping:
            return None
        store = db.query(Store).filter(Store.id == mapping.store_id).first()
        if store:
            db.expunge(store)
        return store


def get_store_by_meta_phone_number_id(phone_number_id: str) -> Store | None:
    """Look up a store by its Meta Cloud API phone number id."""
    with SessionLocal() as db:
        mapping = db.query(StoreMetaNumber).filter(
            StoreMetaNumber.phone_number_id == phone_number_id
        ).first()
        if not mapping:
            return None
        store = db.query(Store).filter(Store.id == mapping.store_id).first()
        if store:
            db.expunge(store)
        return store


def get_meta_access_token(phone_number_id: str) -> str | None:
    """Access token for sending from a Meta phone number (per-store token)."""
    with SessionLocal() as db:
        mapping = db.query(StoreMetaNumber).filter(
            StoreMetaNumber.phone_number_id == phone_number_id
        ).first()
        return mapping.access_token if mapping else None


def get_store_openwa_session(store_id: int) -> StoreOpenWASession | None:
    """Return the OpenWA session row for a store, or None if not configured."""
    with SessionLocal() as db:
        row = db.query(StoreOpenWASession).filter(
            StoreOpenWASession.store_id == store_id
        ).first()
        if row:
            db.expunge(row)
        return row


def get_chain_stores(chain_id: int) -> list[Store]:
    """All locations belonging to a chain."""
    with SessionLocal() as db:
        stores = db.query(Store).filter(Store.chain_id == chain_id).all()
        db.expunge_all()
        return stores


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    logger.info("Central DB tables created/verified")
