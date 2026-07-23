"""Central test configuration.

Sets the SQLite test DB BEFORE any app module is imported so that
`app.core.db.engine` is built against the test DB, not Supabase.
"""
import os

# Must happen before ANY app import
os.environ["DATABASE_URL"] = "sqlite:///./test_central.db"
os.environ["TWILIO_VALIDATE_SIGNATURE"] = "false"
os.environ.setdefault("ZAI_API_KEY", "")          # LLM disabled in tests
os.environ.setdefault("ZAI_MODEL", "glm-4.7")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest1234567890")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-auth-token-xxxx")
os.environ.setdefault("APIFY_TOKEN", "")           # disabled in tests

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.db import Base

TEST_DB_URL = "sqlite:///./test_central.db"

# Build a test engine that every app component will use
test_engine = create_engine(
    TEST_DB_URL, connect_args={"check_same_thread": False}
)
TestSession = sessionmaker(bind=test_engine, autocommit=False, autoflush=False)

# Patch the db module so all internal SessionLocal() calls hit SQLite
import app.core.db as _db_module

_db_module.engine = test_engine
_db_module.SessionLocal = TestSession


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()
    try:
        if os.path.exists("./test_central.db"):
            os.remove("./test_central.db")
    except PermissionError:
        pass  # Windows may hold the file briefly; leftover is harmless


@pytest.fixture(autouse=True)
def _clean_tables(_create_schema):
    """Wipe all rows before each test — ensures full isolation."""
    yield
    with test_engine.connect() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f"DELETE FROM {table.name}"))
        conn.commit()


@pytest.fixture(autouse=True)
def _reset_gateway_guards():
    """Clear the gateway's module-level rate-limit/cooldown/in-flight state.

    Without this, a test that dispatches a message puts its phone number on
    a 4-10s cooldown that silently drops dispatches in the NEXT test using
    the same number. Lazy: only touches the module if a test imported it.
    """
    import sys
    yield
    m = sys.modules.get("app.gateway.main")
    if m is not None:
        m._seen_idem.clear()
        m._customer_last.clear()
        m._customer_inflight.clear()
        m._staff_last.clear()
        m._staff_inflight.clear()
        m._scout_rate.clear()


# ── Convenience helpers ────────────────────────────────────────────────────────

def seed_chain(name="Test Chain"):
    from app.core.db import Chain
    with TestSession() as db:
        c = Chain(name=name)
        db.add(c)
        db.commit()
        db.refresh(c)
        return c.id


def seed_store(chain_id, name="Test Store", location="Main St, Karachi", category="cafe"):
    from app.core.db import Store
    with TestSession() as db:
        s = Store(chain_id=chain_id, name=name, location=location, category=category)
        db.add(s)
        db.commit()
        db.refresh(s)
        store_id = s.id
    # Every existing test predates the per-store agent-entitlement gate
    # (app.core.entitlements) and exercises agent behavior assuming it's
    # simply available -- grandfather every test store into the full
    # package by default (mirrors the real prod backfill for existing
    # stores) so only tests specifically ABOUT entitlement need to touch
    # seed_agent_access() to narrow it.
    seed_agent_access(store_id)
    return store_id


def seed_agent_access(store_id, agents=None):
    """Grants `agents` (default: all 6) to a store. Pass a smaller set
    (or an empty set) to test entitlement-denial behavior."""
    from app.core.entitlements import AGENT_NAMES, set_store_agents
    set_store_agents(store_id, set(AGENT_NAMES) if agents is None else set(agents))


def seed_twilio(store_id, number):
    from app.core.db import StoreTwilioNumber
    with TestSession() as db:
        db.add(StoreTwilioNumber(store_id=store_id, whatsapp_number=number))
        db.commit()


def seed_member(store_id, whatsapp, role="owner"):
    from app.core.db import StoreMember
    with TestSession() as db:
        db.add(StoreMember(store_id=store_id, whatsapp=whatsapp, role=role))
        db.commit()
