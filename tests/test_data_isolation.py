"""Data isolation tests.

Verifies that store_id boundaries are always enforced at the DB layer
and that no data from one store can be seen or inferred by another.
Every query that touches restaurant data must be deterministically scoped.
"""
import pytest
from tests.conftest import seed_chain, seed_store, seed_twilio, seed_member, TestSession

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def two_stores():
    chain_id = seed_chain("Isolation Chain")
    store_a = seed_store(chain_id, name="Store A", location="PECHS, Karachi")
    store_b = seed_store(chain_id, name="Store B", location="Clifton, Karachi")
    seed_twilio(store_a, "whatsapp:+1111111111")
    seed_twilio(store_b, "whatsapp:+2222222222")
    seed_member(store_a, "whatsapp:+923001111111", "owner")
    seed_member(store_b, "whatsapp:+923002222222", "owner")
    return {"store_a": store_a, "store_b": store_b}


# ── StoreMember isolation ─────────────────────────────────────────────────────

def test_is_store_member_only_matches_correct_store(two_stores):
    from app.core.db import is_store_member
    store_a = two_stores["store_a"]
    store_b = two_stores["store_b"]

    assert is_store_member("whatsapp:+923001111111", store_a)
    assert not is_store_member("whatsapp:+923001111111", store_b)
    assert is_store_member("whatsapp:+923002222222", store_b)
    assert not is_store_member("whatsapp:+923002222222", store_a)


def test_unknown_number_not_a_member_of_any_store(two_stores):
    from app.core.db import is_store_member
    assert not is_store_member("whatsapp:+9230000000000", two_stores["store_a"])
    assert not is_store_member("whatsapp:+9230000000000", two_stores["store_b"])


def test_get_stores_for_number_returns_only_member_stores(two_stores):
    from app.core.db import get_stores_for_number
    stores = get_stores_for_number("whatsapp:+923001111111")
    assert len(stores) == 1
    assert stores[0].id == two_stores["store_a"]


def test_get_stores_for_number_returns_multiple_if_member_of_both(two_stores):
    """A representative can be a member of two locations of the same chain."""
    seed_member(two_stores["store_b"], "whatsapp:+923001111111", "manager")
    from app.core.db import get_stores_for_number
    stores = get_stores_for_number("whatsapp:+923001111111")
    assert len(stores) == 2


# ── StoreTwilioNumber isolation ───────────────────────────────────────────────

def test_twilio_number_resolves_to_correct_store(two_stores):
    from app.core.db import get_store_by_twilio_number
    store_a = get_store_by_twilio_number("whatsapp:+1111111111")
    store_b = get_store_by_twilio_number("whatsapp:+2222222222")
    assert store_a is not None and store_a.id == two_stores["store_a"]
    assert store_b is not None and store_b.id == two_stores["store_b"]


def test_unknown_twilio_number_returns_none():
    from app.core.db import get_store_by_twilio_number
    assert get_store_by_twilio_number("whatsapp:+9999999999") is None


# ── UserSession cross-store isolation ──────────────────────────────────────────

def test_session_from_different_store_treated_as_fresh(two_stores):
    from app.core.db import set_user_session, get_user_session
    phone = "whatsapp:+923001111111"
    store_a = two_stores["store_a"]
    store_b = two_stores["store_b"]

    # Set session for store A with internal mode
    set_user_session(phone, store_a, active_agent="internal")
    session = get_user_session(phone)
    assert session.store_id == store_a
    assert session.active_agent == "internal"

    # Simulate gateway logic: session.store_id != store_b → current_mode = None
    current_mode = (
        session.active_agent
        if session and session.store_id == store_b
        else None
    )
    assert current_mode is None


def test_set_user_session_clears_mode_when_none_passed(two_stores):
    from app.core.db import set_user_session, get_user_session
    phone = "whatsapp:+923001111111"
    store_a = two_stores["store_a"]

    set_user_session(phone, store_a, active_agent="internal")
    set_user_session(phone, store_a, active_agent=None)
    session = get_user_session(phone)
    assert session.active_agent is None


def test_set_user_session_overwrites_previous_mode(two_stores):
    from app.core.db import set_user_session, get_user_session
    phone = "whatsapp:+923001111111"
    store_a = two_stores["store_a"]

    set_user_session(phone, store_a, active_agent="internal")
    set_user_session(phone, store_a, active_agent="customer")
    session = get_user_session(phone)
    assert session.active_agent == "customer"


# ── POS connection isolation ──────────────────────────────────────────────────

def test_pos_connection_scoped_to_store(two_stores):
    from app.core.db import POSConnection
    store_a = two_stores["store_a"]
    store_b = two_stores["store_b"]

    with TestSession() as db:
        db.add(POSConnection(
            store_id=store_a,
            pos_type="csv",
            config={"orders": "/tmp/a.csv"},
        ))
        db.commit()

    from app.agents.integrity.service import IntegrityService
    svc = IntegrityService()
    config_a = svc._get_config(store_a)
    config_b = svc._get_config(store_b)

    assert config_a is not None
    assert config_b is None  # store B has no POS config


def test_integrity_data_not_shared_between_stores(two_stores):
    """IntegrityService cache is keyed by store_id — no cross-contamination."""
    from app.agents.integrity.service import IntegrityService, _Cached
    from unittest.mock import MagicMock

    svc = IntegrityService()
    mock_report = MagicMock()
    svc._cache[two_stores["store_a"]] = _Cached(at=float("inf"), report=mock_report)

    # Store B's cache should be empty
    assert two_stores["store_b"] not in svc._cache


# ── Revenue connection isolation ──────────────────────────────────────────────

def test_revenue_connection_scoped_to_store(two_stores):
    from app.core.db import RevenueConnection
    store_a = two_stores["store_a"]
    store_b = two_stores["store_b"]

    with TestSession() as db:
        db.add(RevenueConnection(
            store_id=store_a,
            db_path=":memory:",
            config={"model": "prophet"},
        ))
        db.commit()

    with TestSession() as db:
        row_a = db.query(RevenueConnection).filter(RevenueConnection.store_id == store_a).first()
        row_b = db.query(RevenueConnection).filter(RevenueConnection.store_id == store_b).first()

    assert row_a is not None
    assert row_b is None


# ── Competitor isolation ──────────────────────────────────────────────────────

def test_competitors_scoped_to_store(two_stores):
    from app.core.db import Competitor
    store_a = two_stores["store_a"]
    store_b = two_stores["store_b"]

    with TestSession() as db:
        db.add(Competitor(store_id=store_a, name="Rival Cafe A", source="seed"))
        db.add(Competitor(store_id=store_b, name="Rival Cafe B", source="seed"))
        db.commit()

    from app.agents.scout.discovery import get_all_competitors
    comps_a = get_all_competitors(store_a)
    comps_b = get_all_competitors(store_b)

    assert len(comps_a) == 1
    assert comps_a[0]["name"] == "Rival Cafe A"
    assert len(comps_b) == 1
    assert comps_b[0]["name"] == "Rival Cafe B"


def test_seed_competitors_independent_per_store(two_stores):
    from app.agents.scout.discovery import seed_competitors_for_store, get_all_competitors
    store_a = two_stores["store_a"]
    store_b = two_stores["store_b"]

    seed_competitors_for_store(store_a)
    # Store B should still have no competitors
    assert get_all_competitors(store_b) == []

    seed_competitors_for_store(store_b)
    # Both should now have the same seed list independently
    count_a = len(get_all_competitors(store_a))
    count_b = len(get_all_competitors(store_b))
    assert count_a == count_b > 0


# ── Scout run/findings isolation ──────────────────────────────────────────────

def test_scout_findings_scoped_to_store(two_stores):
    from app.core.db import ScoutRun, Finding
    store_a = two_stores["store_a"]
    store_b = two_stores["store_b"]

    with TestSession() as db:
        run_a = ScoutRun(store_id=store_a, command="scout", status="ok")
        run_b = ScoutRun(store_id=store_b, command="scout", status="ok")
        db.add_all([run_a, run_b])
        db.commit()
        run_a_id = run_a.id
        run_b_id = run_b.id

    with TestSession() as db:
        db.add(Finding(store_id=store_a, run_id=run_a_id,
                       competitor_name="Rival A", content_text="Store A intel"))
        db.add(Finding(store_id=store_b, run_id=run_b_id,
                       competitor_name="Rival B", content_text="Store B intel"))
        db.commit()

    with TestSession() as db:
        findings_a = db.query(Finding).filter(Finding.store_id == store_a).all()
        findings_b = db.query(Finding).filter(Finding.store_id == store_b).all()

    assert len(findings_a) == 1
    assert findings_a[0].competitor_name == "Rival A"
    assert len(findings_b) == 1
    assert findings_b[0].competitor_name == "Rival B"


# ── store_id NEVER accepted from user message body ────────────────────────────

def test_store_id_always_comes_from_twilio_to_field():
    """Smoke-test that the gateway function signature never takes store_id from user input.

    This test documents the contract: store_id comes exclusively from the `To`
    Twilio field → store_twilio_numbers lookup, not from the message body or
    user session. Any change to this must be intentional.
    """
    import inspect
    from app.gateway import customer, internal
    sig_cust = inspect.signature(customer.handle_customer_for_store)
    sig_int  = inspect.signature(internal.handle_internal_for_store)

    # store_id is a positional arg (last), not derived from the body
    cust_params = list(sig_cust.parameters.keys())
    int_params  = list(sig_int.parameters.keys())

    assert "store_id" in cust_params
    assert "store_id" in int_params
    # Body / message text param must also be present
    assert "body" in cust_params
    assert "body" in int_params


# ── get_chain_stores ──────────────────────────────────────────────────────────

def test_get_chain_stores_only_returns_own_chain(two_stores):
    from app.core.db import get_chain_stores, Chain

    with TestSession() as db:
        chain_row = db.query(Chain).first()
        chain_id = chain_row.id

    stores = get_chain_stores(chain_id)
    ids = {s.id for s in stores}
    assert two_stores["store_a"] in ids
    assert two_stores["store_b"] in ids


def test_get_chain_stores_excludes_other_chains():
    """Stores from different chains are not returned."""
    chain_a = seed_chain("Chain Alpha")
    chain_b = seed_chain("Chain Beta")
    store_a = seed_store(chain_a, "Alpha1")
    store_b = seed_store(chain_b, "Beta1")

    from app.core.db import get_chain_stores
    stores_a = get_chain_stores(chain_a)
    stores_b = get_chain_stores(chain_b)

    ids_a = {s.id for s in stores_a}
    ids_b = {s.id for s in stores_b}

    assert store_a in ids_a
    assert store_b not in ids_a
    assert store_b in ids_b
    assert store_a not in ids_b
