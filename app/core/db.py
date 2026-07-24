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
    Boolean, Column, Date, DateTime, Float, ForeignKey, Integer,
    JSON, String, Text, UniqueConstraint, create_engine,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from app.core.config import DATABASE_URL

logger = logging.getLogger(__name__)

_kw = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
_is_sqlite = "sqlite" in DATABASE_URL
# pool_size/max_overflow are explicit (not SQLAlchemy's 5+10 default) so the
# per-worker ceiling is a known, chosen number: WEB_CONCURRENCY worker
# processes (scripts/run_server.py, kept at 4 -- see its comment on why 8
# crashed the container) each get their own engine/pool, so the system-wide
# max is pool_size+max_overflow times worker count. Sized so that total
# stays comfortably under Postgres's actual max_connections (confirmed
# live: 100) with headroom for admin/migration connections -- 20/worker x
# 4 workers = 80, leaving 20 free. SQLite (dev/tests) has no such pool.
_pool_kw = {} if _is_sqlite else {"pool_size": 10, "max_overflow": 10}
engine = create_engine(
    DATABASE_URL,
    connect_args=_kw,
    pool_pre_ping=True,   # test connection before use; reconnects if the DB dropped it
    pool_recycle=300,     # recycle connections every 5 min to avoid SSL EOF on idle
    **_pool_kw,
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
    # Free-text identity/context for this restaurant (cuisine, vibe, what
    # makes it distinct, target customers, etc) -- surfaced to staff-facing
    # LLM prompts via app.core.persona.staff_persona() so every agent's
    # answer is grounded in what this specific restaurant actually is, not
    # just its name. Nullable: existing stores have none until set.
    description = Column(Text, nullable=True)
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

    location_id: which branch this member is restricted to (owner is
    never restricted regardless of this value; manager/staff with no
    location_id assigned yet have no branch to act on until the owner
    assigns one from the dashboard). NULL for stores that don't use
    maitre_d's multi-branch feature at all. Deliberately no FK to
    maitre_d_locations -- StoreMember is a general cross-agent table,
    this column is only meaningful when maitre_d's branch model applies.
    """
    __tablename__ = "store_members"
    __table_args__ = (UniqueConstraint("store_id", "whatsapp"),)

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    whatsapp = Column(String, nullable=False)
    role = Column(String, default="owner")
    location_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class StoreAgentAccess(Base):
    """Which of the 6 agents (integrity, revenue, scout, reputation,
    maitre_d, customer) a store's package includes. One row per granted
    agent -- a store with NO rows here has access to NOTHING. Deliberately
    fail-CLOSED, unlike most other per-store tables in this file (whose
    "no row yet" default is some sensible always-on behavior): entitlement
    is the one thing here that must never silently default to "on" just
    because nobody explicitly configured it yet. See app.core.entitlements
    for the single source of truth that reads/writes this table -- every
    staff/customer dispatch point (WhatsApp, the staff web dashboard, and
    background crons) checks it before running any agent logic."""
    __tablename__ = "store_agent_access"
    __table_args__ = (UniqueConstraint("store_id", "agent"),)

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    agent = Column(String, nullable=False)
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
    # JSON-encoded list[float] (all-MiniLM-L6-v2, 384-dim), not a native
    # vector column -- unlike the KB's pgvector setup (a DB-native addition
    # outside this repo's tracked schema), this stays plain Text so it
    # works identically on SQLite (tests) and Postgres (prod) with no
    # extension dependency. Similarity is computed in Python (same pattern
    # as app/agents/customer/community/intent.py's menu-intent matching),
    # not in SQL -- fine at the scale reviews/findings actually reach
    # (hundreds, not millions) per store. NULL until backfilled/computed at
    # write time; rows without one are simply skipped by semantic search.
    content_embedding = Column(Text, nullable=True)


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
    reward_text = Column(String, nullable=False, default="a free treat")
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
# Maitre D agent (live walk-in queue, VIP handling)
# ---------------------------------------------------------------------------
# Ported from the standalone maitre-d-agent branch (single-tenant SQLite,
# its own FastAPI app) into this server's shared, store_id-partitioned
# Postgres schema -- same pattern as every other agent's tables here.

class MaitreDLocation(Base):
    """One row per physical branch a store runs a queue at -- a store with
    several branches (like Anatummy's three) gets one row each, so guests
    at one branch are never conflated with a different physical address.
    A store with a single location still gets exactly one row here
    (is_primary=True); app.agents.maitre_d.config falls back to sensible
    defaults in code for a store with none configured yet, same pattern as
    POSConnection/RevenueConnection.

    accepts_reservations lets a delivery-only branch (no dine-in seating
    at all) be excluded from the branch choice offered to a guest, rather
    than pretending it has a queue to join.

    tables/service_windows/turn_time_minutes/large_party_*/max_party_size/
    currency/deposit_amount/offer_ttl_minutes/no_show_grace_minutes/
    reminder_lead_hours are legacy columns from the earlier date/time
    table-reservation model (deposits, no-show scoring, service windows) --
    left in place rather than migrated away since dropping columns is
    riskier than just retiring the code that read them; app.agents.
    maitre_d.config.VenueConfig no longer reads any of them."""
    __tablename__ = "maitre_d_locations"
    __table_args__ = (UniqueConstraint("store_id", "branch_key"),)

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    branch_key = Column(String, nullable=False)   # short slug, e.g. "new_blue_area"
    name = Column(String, nullable=False)          # display name, e.g. "New Blue Area"
    address = Column(String, default="")
    accepts_reservations = Column(Boolean, default=True)
    is_primary = Column(Boolean, default=False)    # the default when a store has only one, or the fallback
    booking_enabled = Column(Boolean, nullable=False, default=True)  # this branch's own "disable booking" toggle
    timezone = Column(String, nullable=False, default="Asia/Karachi")
    tables = Column(JSON, default=list)              # legacy, unused -- see class docstring
    service_windows = Column(JSON, default=list)      # legacy, unused
    turn_time_minutes = Column(Integer, default=90)   # legacy, unused
    large_party_turn_minutes = Column(Integer, default=120)  # legacy, unused
    large_party_threshold = Column(Integer, default=6)       # legacy, unused
    max_party_size = Column(Integer, default=12)      # legacy, unused
    currency = Column(String, default="PKR")          # legacy, unused
    deposit_amount = Column(Integer, default=1000)    # legacy, unused
    offer_ttl_minutes = Column(Integer, default=15)   # legacy, unused
    no_show_grace_minutes = Column(Integer, default=30)  # legacy, unused
    reminder_lead_hours = Column(Integer, default=24)    # legacy, unused
    conversation_ttl_minutes = Column(Integer, default=180)
    created_at = Column(DateTime, default=datetime.utcnow)


class MaitreDVip(Base):
    """A store's manually-curated VIP list, phone -> profile. Replaces the
    original branch's config-file dict so staff can manage it live."""
    __tablename__ = "maitre_d_vips"
    __table_args__ = (UniqueConstraint("store_id", "phone"),)

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    phone = Column(String, nullable=False)   # normalised E.164, no "whatsapp:" prefix
    name = Column(String, default="")
    tier = Column(String, default="vip")     # "vip" | "regular" | "press" | "owner_friend" ...
    notes = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class MaitreDGuest(Base):
    __tablename__ = "maitre_d_guests"
    __table_args__ = (UniqueConstraint("store_id", "phone"),)

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    phone = Column(String, nullable=False)
    name = Column(String, default="")
    vip_tier = Column(String, default="")
    vip_notes = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class MaitreDQueueEntry(Base):
    """One row per guest waiting in a branch's live walk-in queue.

    Replaces the earlier date/time table-reservation model (formerly
    MaitreDReservation/MaitreDWaitlist -- those Postgres tables are left in
    place, unmanaged, rather than dropped, since a schema drop is riskier
    than simply retiring the ORM classes that wrote to them; nothing in the
    app reads or writes them any more). A store like Anatummy is walk-in
    first, so "book" now means "join today's queue and get a number", not
    "reserve a future time slot" -- no tables, service windows, deposits or
    no-show scoring apply any more.

    queue_number is permanent for the day and never reused (it's what the
    guest is told back). position is the live 1-based ordering among a
    location's "waiting" rows -- the thing admit/remove/insert reshuffle."""
    __tablename__ = "maitre_d_queue"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    location_id = Column(Integer, ForeignKey("maitre_d_locations.id"), nullable=True)
    branch_name = Column(String, default="")  # denormalised at join time -- avoids a join on every listing
    queue_number = Column(Integer, nullable=False)
    phone = Column(String, nullable=False)
    name = Column(String, default="")
    party_size = Column(Integer, default=1)
    special_requests = Column(Text, default="")
    status = Column(String, nullable=False, default="waiting")  # waiting | admitted | removed | cancelled
    position = Column(Integer, default=0)  # 1-based, dense among this location's "waiting" rows
    is_vip = Column(Boolean, default=False)
    vip_tier = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    admitted_at = Column(DateTime, nullable=True)


class MaitreDConversation(Base):
    """Slot-filling state for an in-progress booking, per store+phone --
    separate from the staff/customer chat-session history tables since this
    holds structured flow state (which slots are filled), not a message
    transcript."""
    __tablename__ = "maitre_d_conversations"
    __table_args__ = (UniqueConstraint("store_id", "phone"),)

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    phone = Column(String, nullable=False)
    state = Column(JSON, default=dict)
    updated_at = Column(DateTime, default=datetime.utcnow)


class MaitreDSettings(Base):
    """One row per store of toggle-able queue settings. A store with no
    row yet behaves as if every field below were at its default (see
    app.agents.maitre_d.config's getters) -- no admin step required
    before the queue works, same pattern as every other maitre_d table
    here.

    booking_enabled: staff's "disable booking"/"enable booking" command,
    for a quiet day where they're seating people directly instead of
    running the queue.

    seated_grace_minutes: how long after being admitted a guest is still
    treated as "currently dining" and blocked from rejoining the queue
    (staff's "seated grace <N>" command).

    queue_stale_minutes: how long a "waiting" entry can sit with no staff
    action before the maintenance sweep assumes the guest isn't coming
    and releases the spot (staff's "queue timeout <N>" command).

    entrance_code_ttl_minutes: how long a one-time entrance code minted by
    the /q/{store_id} relinker stays redeemable (staff's "entrance code
    ttl <N>" command) -- see MaitreDEntranceCode."""
    __tablename__ = "maitre_d_settings"

    store_id = Column(Integer, ForeignKey("stores.id"), primary_key=True)
    booking_enabled = Column(Boolean, nullable=False, default=True)
    seated_grace_minutes = Column(Integer, nullable=False, default=120)
    queue_stale_minutes = Column(Integer, nullable=False, default=90)
    entrance_code_ttl_minutes = Column(Integer, nullable=False, default=15)


class MaitreDEntranceCode(Base):
    """A one-time code minted on each visit to the /q/{store_id} relinker
    (see gateway/main.py's entrance_qr_relink) and embedded in the wa.me
    text it redirects to. The PRINTED QR sticker is static -- it encodes
    the relinker URL, never a code -- but every scan mints a fresh,
    single-use code server-side, so a screenshot or memorised copy of a
    past "Join the Queue ... #CODE" message stops working the moment
    either the code is redeemed once or its TTL passes. This is what
    closes the "someone at home replays the trigger text" gap that the
    fixed trigger PHRASE alone (gateway/customer.py's BOOKING_TRIGGER_
    PHRASE) never could -- the phrase is meant to be public/printable,
    the code is meant to be single-use and short-lived.

    Deliberately its own table rather than reusing MaitreDQueueEntry or
    MaitreDConversation -- a code exists before we know anything about
    who's about to use it (no phone number yet), and must be checked
    without ever having created a queue entry for a redemption that
    turns out to be invalid."""
    __tablename__ = "maitre_d_entrance_codes"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False, index=True)
    location_id = Column(Integer, nullable=True)  # None = store's single implicit location
    code = Column(String, nullable=False, unique=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    used_at = Column(DateTime, nullable=True)


class MaitreDQueueCounter(Base):
    """Serialization anchor + daily sequence counter for one store
    location's live queue. Every queue-mutating operation (join, admit,
    remove, insert) locks this ONE row first (SELECT ... FOR UPDATE,
    creating it if needed) before touching any MaitreDQueueEntry rows for
    that location -- this is what makes queue_number allocation and
    position shifting safe under concurrent requests (multiple worker
    processes handling simultaneous WhatsApp messages): Postgres blocks a
    second transaction from acquiring the same row lock until the first
    commits, so two people scanning the entrance QR in the same instant
    can never be assigned the same number or position.

    location_id is never NULL here (0 means "the store's single implicit
    location") -- unlike MaitreDQueueEntry, this row needs a reliable,
    collision-free unique key, and Postgres treats every NULL as distinct
    from every other NULL in a unique constraint, which would silently
    defeat the whole point of locking it."""
    __tablename__ = "maitre_d_queue_counters"
    __table_args__ = (UniqueConstraint("store_id", "location_id"),)

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), nullable=False)
    location_id = Column(Integer, nullable=False, default=0)
    last_number = Column(Integer, nullable=False, default=0)
    last_day = Column(Date, nullable=True)


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
    """Create any tables that don't exist yet. scripts/run_server.py calls
    this once, before spawning worker processes -- that's the primary
    guard. Also called per-worker from app/gateway/main.py's lifespan()
    as a safety net for anything started outside run_server.py, so this
    stays resilient to being invoked concurrently from multiple processes
    too: confirmed live, several workers all calling create_all() at once
    the first time a brand-new table appears raced Postgres's own
    catalog and crashed startup with "duplicate key value violates unique
    constraint pg_type_typname_nsp_index" (SQLAlchemy's create_all does
    its own existence check, but that check-then-create isn't atomic
    across processes). Once a table exists, create_all() is a no-op for
    it, so this can only race on genuinely new tables, and only until the
    first process wins."""
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as exc:
        if "already exists" in str(exc) or "duplicate key" in str(exc):
            logger.info("Central DB tables: another process created them concurrently (%s)", exc.__class__.__name__)
        else:
            raise
    logger.info("Central DB tables created/verified")
