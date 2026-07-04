"""Gateway routing tests — unified /whatsapp webhook and admin endpoints.

Every scenario: unknown number, customer flow, staff mode selection,
mode switching, cross-store session isolation, all admin CRUD endpoints.
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from tests.conftest import seed_chain, seed_store, seed_twilio, seed_member

# ── Fixtures ──────────────────────────────────────────────────────────────────

STORE_NUMBER_A = "whatsapp:+11111111111"
STORE_NUMBER_B = "whatsapp:+22222222222"
STAFF_PHONE    = "whatsapp:+923001234567"
CUSTOMER_PHONE = "whatsapp:+923009999999"


@pytest.fixture
def db_state():
    """Seed two stores, one twilio number each, one staff member on store A only."""
    chain_id = seed_chain("Test Chain")
    store_a   = seed_store(chain_id, name="Alpha Cafe", location="DHA, Lahore")
    store_b   = seed_store(chain_id, name="Beta Cafe",  location="Gulberg, Lahore")
    seed_twilio(store_a, STORE_NUMBER_A)
    seed_twilio(store_b, STORE_NUMBER_B)
    seed_member(store_a, STAFF_PHONE, role="owner")
    return {"store_a": store_a, "store_b": store_b, "chain_id": chain_id}


@pytest.fixture
def client():
    from app.gateway.main import app
    with TestClient(app) as c:
        yield c


def _post(client, from_num, to_num, body):
    from urllib.parse import urlencode
    payload = urlencode({"From": from_num, "To": to_num, "Body": body})
    return client.post(
        "/whatsapp",
        content=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


# ── Helper: inspect TwiML body ────────────────────────────────────────────────

def _body(resp) -> str:
    """Extract the text inside <Body>...</Body> from a TwiML response."""
    import re
    m = re.search(r"<Body>(.*?)</Body>", resp.text, re.DOTALL)
    return m.group(1) if m else ""


# ── Unknown / unconfigured number ─────────────────────────────────────────────

def test_unknown_twilio_number_returns_empty_response(client, db_state):
    r = _post(client, CUSTOMER_PHONE, "whatsapp:+99999999999", "Hi")
    assert r.status_code == 200
    assert "<Response></Response>" in r.text


# ── Customer flow (not a staff member) ───────────────────────────────────────

def test_customer_gets_customer_agent(client, db_state):
    # Customer replies are dispatched async: the webhook ack is empty TwiML
    # and the real reply goes out via the Twilio outbound API.
    with (
        patch("app.gateway.customer.handle_customer_for_store", return_value="Hi there!") as mock,
        patch("app.gateway.main._send_outbound") as mock_send,
    ):
        r = _post(client, CUSTOMER_PHONE, STORE_NUMBER_A, "hello")
    assert r.status_code == 200
    assert "<Response></Response>" in r.text
    mock.assert_called_once_with(CUSTOMER_PHONE, "hello", db_state["store_a"])
    mock_send.assert_called_once_with(
        to=CUSTOMER_PHONE, from_=STORE_NUMBER_A, body="Hi there!"
    )


def test_customer_routed_to_correct_store(client, db_state):
    with (
        patch("app.gateway.customer.handle_customer_for_store", return_value="Store B reply") as mock,
        patch("app.gateway.main._send_outbound"),
    ):
        r = _post(client, CUSTOMER_PHONE, STORE_NUMBER_B, "hi")
    mock.assert_called_once_with(CUSTOMER_PHONE, "hi", db_state["store_b"])


def test_customer_empty_reply_returns_empty_twiml(client, db_state):
    with patch("app.gateway.customer.handle_customer_for_store", return_value=""):
        r = _post(client, CUSTOMER_PHONE, STORE_NUMBER_A, "opt out")
    assert "<Response></Response>" in r.text


# ── Staff first contact → mode-selection menu ─────────────────────────────────

def test_staff_first_message_gets_mode_menu(client, db_state):
    r = _post(client, STAFF_PHONE, STORE_NUMBER_A, "hi")
    assert r.status_code == 200
    body = _body(r)
    assert "Staff tools" in body or "1" in body
    assert "Customer app" in body or "2" in body


def test_staff_unknown_message_gets_mode_menu(client, db_state):
    r = _post(client, STAFF_PHONE, STORE_NUMBER_A, "What are the sales?")
    body = _body(r)
    assert "1" in body and "2" in body


# ── Staff selects mode ────────────────────────────────────────────────────────

def test_staff_selects_1_enters_internal_mode(client, db_state):
    r = _post(client, STAFF_PHONE, STORE_NUMBER_A, "1")
    body = _body(r)
    assert "Staff tools" in body or "Integrity" in body
    # Session now in internal mode — next message is ack'd synchronously and
    # the real reply is delivered async via the Twilio outbound API.
    with (
        patch("app.gateway.internal.handle_internal_for_store", return_value="audit result") as mock,
        patch("app.gateway.main._send_outbound") as mock_send,
    ):
        _post(client, STAFF_PHONE, STORE_NUMBER_A, "summary")
    mock.assert_called_once()
    mock_send.assert_called_once_with(
        to=STAFF_PHONE, from_=STORE_NUMBER_A, body="audit result"
    )


def test_staff_selects_2_enters_customer_mode(client, db_state):
    r = _post(client, STAFF_PHONE, STORE_NUMBER_A, "2")
    body = _body(r)
    assert "customer" in body.lower() or "stamp" in body.lower() or "switched" in body.lower()
    # Next message routes to customer agent (async dispatch, outbound delivery)
    with (
        patch("app.gateway.customer.handle_customer_for_store", return_value="loyalty reply") as mock,
        patch("app.gateway.main._send_outbound") as mock_send,
    ):
        _post(client, STAFF_PHONE, STORE_NUMBER_A, "my stamps")
    mock.assert_called_once()
    mock_send.assert_called_once_with(
        to=STAFF_PHONE, from_=STORE_NUMBER_A, body="loyalty reply"
    )


def test_staff_invalid_mode_selection_stays_on_menu(client, db_state):
    r = _post(client, STAFF_PHONE, STORE_NUMBER_A, "3")
    body = _body(r)
    assert "1" in body and "2" in body  # menu shown again


# ── Internal mode — transparent agent routing ──────────────────────────────────

def test_staff_internal_integrity_message_routed(client, db_state):
    _post(client, STAFF_PHONE, STORE_NUMBER_A, "1")  # enter internal mode
    with (
        patch("app.gateway.internal.handle_internal_for_store", return_value="leakage PKR 5000") as mock,
        patch("app.gateway.main._send_outbound") as mock_send,
    ):
        _post(client, STAFF_PHONE, STORE_NUMBER_A, "leakage")
    mock.assert_called_once_with(STAFF_PHONE, "leakage", db_state["store_a"])
    mock_send.assert_called_once_with(
        to=STAFF_PHONE, from_=STORE_NUMBER_A, body="leakage PKR 5000"
    )


def test_staff_internal_scout_message_routed(client, db_state):
    # Scout is dispatched async in the gateway (not via handle_internal_for_store).
    # The immediate TwiML response contains an "on it" acknowledgement.
    _post(client, STAFF_PHONE, STORE_NUMBER_A, "1")
    with patch("app.gateway.main._bg_scout") as mock_bg:
        r = _post(client, STAFF_PHONE, STORE_NUMBER_A, "scout")
    # Background task should be registered (called once with correct store_id)
    mock_bg.assert_called_once()
    args = mock_bg.call_args[0]
    assert args[0] == db_state["store_a"]  # store_id
    assert args[1] == STAFF_PHONE           # from_number
    # Immediate reply should mention the wait time
    body = _body(r)
    assert "7-10" in body or "minutes" in body or "competitors" in body.lower()


def test_staff_internal_revenue_message_routed(client, db_state):
    _post(client, STAFF_PHONE, STORE_NUMBER_A, "1")
    with (
        patch("app.gateway.internal.handle_internal_for_store", return_value="Revenue forecast") as mock,
        patch("app.gateway.main._send_outbound"),
    ):
        _post(client, STAFF_PHONE, STORE_NUMBER_A, "revenue")
    mock.assert_called_once_with(STAFF_PHONE, "revenue", db_state["store_a"])


# ── Mode triggers (menu / back / switch) ──────────────────────────────────────

@pytest.mark.parametrize("trigger", ["menu", "back", "home", "switch", "mode",
                                      "MENU", "Back", "HOME"])
def test_mode_trigger_resets_to_selection_screen(client, db_state, trigger):
    _post(client, STAFF_PHONE, STORE_NUMBER_A, "1")  # enter internal mode first
    r = _post(client, STAFF_PHONE, STORE_NUMBER_A, trigger)
    body = _body(r)
    assert "1" in body and "2" in body  # mode menu shown


def test_menu_trigger_clears_session(client, db_state):
    _post(client, STAFF_PHONE, STORE_NUMBER_A, "1")
    _post(client, STAFF_PHONE, STORE_NUMBER_A, "menu")
    # After menu, sending "1" again should take them to internal mode (not bypass to agent)
    r = _post(client, STAFF_PHONE, STORE_NUMBER_A, "1")
    body = _body(r)
    assert "Staff tools" in body or "Integrity" in body


# ── Cross-store session isolation ─────────────────────────────────────────────

def test_staff_cross_store_session_resets_mode(client, db_state):
    """Staff sets internal mode for store A, then texts store B — should get mode menu."""
    _post(client, STAFF_PHONE, STORE_NUMBER_A, "1")  # internal mode for store A

    # Now text store B (STAFF_PHONE is NOT a member of store B, so they're a customer there)
    with patch("app.gateway.customer.handle_customer_for_store", return_value="customer reply") as mock:
        r = _post(client, STAFF_PHONE, STORE_NUMBER_B, "summary")
    # Not a member of store B — treated as customer
    mock.assert_called_once_with(STAFF_PHONE, "summary", db_state["store_b"])


def test_staff_session_does_not_leak_between_stores(client, db_state):
    """Add STAFF_PHONE to store B too — they should get a fresh mode-selection screen."""
    seed_member(db_state["store_b"], STAFF_PHONE, role="manager")
    _post(client, STAFF_PHONE, STORE_NUMBER_A, "1")  # internal mode for store A

    # Text store B — different store, session should be treated as new
    r = _post(client, STAFF_PHONE, STORE_NUMBER_B, "summary")
    body = _body(r)
    # Should get mode-selection menu (cross-store session cleared)
    assert "1" in body and "2" in body


# ── Health endpoint ───────────────────────────────────────────────────────────

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert "status" in data
    assert data["status"] in ("ok", "degraded")  # degraded when API keys not set in test env
    assert "agents" in data
    assert "checks" in data
    # All agent modules must import cleanly regardless of API key state
    for name in ("scout", "integrity", "reputation", "revenue", "customer"):
        assert name in data["agents"], f"Agent {name!r} missing from health response"
        assert data["agents"][name] == "ok", f"Agent {name!r} failed to import: {data['agents'][name]}"


# ── Admin endpoints ───────────────────────────────────────────────────────────

def test_admin_create_chain(client):
    r = client.post("/admin/chains",
                    data="name=Test+Chain",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "Test Chain"
    assert "id" in data


def test_admin_create_chain_requires_name(client):
    r = client.post("/admin/chains", data="name=",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 400


def test_admin_create_store(client):
    r_chain = client.post("/admin/chains",
                          data="name=Chai+House",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    chain_id = r_chain.json()["id"]

    r = client.post("/admin/stores",
                    data=f"name=F-7+Branch&chain_id={chain_id}&category=cafe&location=F-7%2C+Islamabad",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "F-7 Branch"
    assert "id" in data


def test_admin_create_store_requires_name(client):
    r = client.post("/admin/stores",
                    data="name=",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 400


def test_admin_add_member(client, db_state):
    from urllib.parse import urlencode
    r = client.post(f"/admin/stores/{db_state['store_a']}/members",
                    content=urlencode({"whatsapp": "whatsapp:+923110001111", "role": "manager"}),
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 201
    data = r.json()
    assert data["status"] == "added"

    r2 = client.post(f"/admin/stores/{db_state['store_a']}/members",
                     content=urlencode({"whatsapp": "whatsapp:+923110001111", "role": "manager"}),
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r2.json()["status"] == "already_exists"


def test_admin_add_member_unknown_store(client):
    r = client.post("/admin/stores/99999/members",
                    data="whatsapp=whatsapp%3A%2B1234&role=owner",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 404


def test_admin_set_twilio_number(client, db_state):
    from urllib.parse import urlencode
    r = client.post(f"/admin/stores/{db_state['store_a']}/twilio",
                    content=urlencode({"whatsapp_number": "whatsapp:+13001234567"}),
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 201
    data = r.json()
    assert data["whatsapp_number"] == "whatsapp:+13001234567"


def test_admin_set_twilio_number_requires_number(client, db_state):
    r = client.post(f"/admin/stores/{db_state['store_a']}/twilio",
                    data="whatsapp_number=",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 400


def test_admin_set_twilio_updates_existing(client, db_state):
    from urllib.parse import urlencode
    store_id = db_state["store_a"]
    client.post(f"/admin/stores/{store_id}/twilio",
                content=urlencode({"whatsapp_number": "whatsapp:+1111"}),
                headers={"Content-Type": "application/x-www-form-urlencoded"})
    r = client.post(f"/admin/stores/{store_id}/twilio",
                    content=urlencode({"whatsapp_number": "whatsapp:+2222"}),
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    data = r.json()
    assert data["whatsapp_number"] == "whatsapp:+2222"


def test_admin_configure_pos(client, db_state):
    import json
    config = json.dumps({"orders": "data/sales_detail.csv",
                         "menu": "data/menu.csv",
                         "staff": "data/staff.csv"})
    r = client.post(f"/admin/stores/{db_state['store_a']}/pos",
                    data=f"pos_type=csv&config={config}&mapping=cafe_generic",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 201
    assert r.json()["status"] == "configured"


def test_admin_configure_pos_unknown_store(client):
    r = client.post("/admin/stores/99999/pos",
                    data='pos_type=csv&config={}',
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 404


def test_admin_list_stores(client, db_state):
    r = client.get("/admin/stores")
    assert r.status_code == 200
    stores = r.json()
    assert len(stores) == 2
    names = {s["name"] for s in stores}
    assert "Alpha Cafe" in names
    assert "Beta Cafe" in names


def test_admin_get_store(client, db_state):
    store_id = db_state["store_a"]
    r = client.get(f"/admin/stores/{store_id}")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == store_id
    assert data["name"] == "Alpha Cafe"
    assert any(m["whatsapp"] == STAFF_PHONE for m in data["members"])
    assert data["whatsapp_number"] == STORE_NUMBER_A
    assert data["pos_configured"] is False


def test_admin_get_store_not_found(client):
    r = client.get("/admin/stores/99999")
    assert r.status_code == 404


def test_admin_add_location(client, db_state):
    r = client.post(f"/admin/stores/{db_state['store_a']}/locations",
                    data="address=Shop+3%2C+Main+Blvd&city=Lahore&area=DHA+Phase+5&is_primary=true",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 201
    assert r.json()["status"] == "added"


def test_admin_add_location_requires_address(client, db_state):
    r = client.post(f"/admin/stores/{db_state['store_a']}/locations",
                    data="address=",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 400


def test_admin_seed_competitors(client, db_state):
    r = client.post(f"/admin/stores/{db_state['store_a']}/seed-competitors")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "seeded"
    assert data["added"] > 0


def test_admin_seed_competitors_idempotent(client, db_state):
    client.post(f"/admin/stores/{db_state['store_a']}/seed-competitors")
    r = client.post(f"/admin/stores/{db_state['store_a']}/seed-competitors")
    assert r.json()["added"] == 0  # already seeded, no new additions


def test_admin_seed_competitors_unknown_store(client):
    r = client.post("/admin/stores/99999/seed-competitors")
    assert r.status_code == 404


def test_admin_configure_revenue(client, db_state):
    import json
    config = json.dumps({"model": "prophet"})
    r = client.post(f"/admin/stores/{db_state['store_a']}/revenue",
                    data=f"config={config}&db_path=:memory:",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 201
    assert r.json()["status"] == "configured"


def test_admin_get_store_shows_pos_configured(client, db_state):
    import json
    store_id = db_state["store_a"]
    config = json.dumps({})
    client.post(f"/admin/stores/{store_id}/pos",
                data=f"pos_type=csv&config={config}",
                headers={"Content-Type": "application/x-www-form-urlencoded"})
    r = client.get(f"/admin/stores/{store_id}")
    assert r.json()["pos_configured"] is True
