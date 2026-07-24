"""Staff web dashboard — WhatsApp-OTP login, per-store session scoping,
and every queue action (admit/remove/insert/VIP/settings) producing the
exact same state changes as the WhatsApp staff commands, since both paths
call the same underlying functions.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import seed_chain, seed_store, seed_member

STAFF_PHONE = "whatsapp:+923001234567"
STAFF_PHONE_RAW = "+923001234567"  # what a human actually types into the login form


@pytest.fixture
def fake_redis(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    import app.core.cache as cache
    monkeypatch.setattr(cache, "_client", fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(cache, "_unavailable", False)


@pytest.fixture
def client(fake_redis):
    from app.gateway.main import app
    # base_url must be https:// -- the session cookie is Secure-flagged
    # (as it should be in production), and a client won't store/resend a
    # Secure cookie against a plain http:// origin, which TestClient uses
    # by default.
    with TestClient(app, base_url="https://testserver") as c:
        yield c


@pytest.fixture
def store_id():
    chain_id = seed_chain("Dashboard Test Chain")
    sid = seed_store(chain_id, name="Dashboard Test Cafe", location="F-7, Islamabad")
    seed_member(sid, STAFF_PHONE, role="owner")
    return sid


def _otp_code(whatsapp_id: str) -> str:
    from app.core import cache
    entry = cache.get(f"dashboard_otp:{whatsapp_id}")
    assert entry, "expected an OTP to have been generated"
    return entry["code"]


def _login(client, store_id) -> None:
    """Full login flow for the single-store STAFF_PHONE, leaving `client`
    with a valid session cookie scoped to `store_id`."""
    client.post("/dashboard/login", data={"phone": STAFF_PHONE_RAW})
    code = _otp_code(STAFF_PHONE)
    r = client.post("/dashboard/verify", data={"phone": STAFF_PHONE, "code": code}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard/queue"


# ── auth flow ────────────────────────────────────────────────────────────────

class TestDashboardAuth:
    def test_unauthenticated_queue_request_redirects_to_login(self, client, store_id):
        r = client.get("/dashboard/queue", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/dashboard/login"

    def test_unregistered_phone_gets_the_same_generic_response(self, client, store_id):
        """Enumeration guard: the response must not reveal whether a phone
        is actually registered staff."""
        r_unregistered = client.post("/dashboard/login", data={"phone": "+923009999999"})
        r_registered = client.post("/dashboard/login", data={"phone": STAFF_PHONE_RAW})
        assert r_unregistered.status_code == r_registered.status_code
        # Only the registered number actually got a code generated.
        from app.core import cache
        assert cache.get("dashboard_otp:whatsapp:+923009999999") is None
        assert cache.get(f"dashboard_otp:{STAFF_PHONE}") is not None

    def test_wrong_code_is_rejected(self, client, store_id):
        client.post("/dashboard/login", data={"phone": STAFF_PHONE_RAW})
        r = client.post("/dashboard/verify", data={"phone": STAFF_PHONE, "code": "000000"})
        assert r.status_code == 400
        assert "wrong" in r.text.lower()

    def test_code_is_single_use(self, client, store_id):
        client.post("/dashboard/login", data={"phone": STAFF_PHONE_RAW})
        code = _otp_code(STAFF_PHONE)
        client.post("/dashboard/verify", data={"phone": STAFF_PHONE, "code": code})
        second = client.post("/dashboard/verify", data={"phone": STAFF_PHONE, "code": code})
        assert second.status_code == 400

    def test_full_browser_redirect_chain_preserves_the_plus_sign(self, client, store_id):
        """Regression test for a real production bug: login_submit's
        redirect used to interpolate the "whatsapp:+<digits>" id into the
        query string unencoded, so the "+" got silently decoded back to a
        space by the time verify_page/verify_submit parsed it, permanently
        breaking every real login. Unlike the other tests in this file
        (which POST straight to /dashboard/verify with a hardcoded correct
        phone, never exercising the redirect at all), this one follows the
        actual browser path: submit login, follow the 303 to /verify,
        extract whatever phone value the server actually put in the
        hidden form field, and submit that -- exactly what a browser
        does."""
        r = client.post("/dashboard/login", data={"phone": STAFF_PHONE_RAW}, follow_redirects=False)
        assert r.status_code == 303
        verify_page = client.get(r.headers["location"])
        assert verify_page.status_code == 200
        import re as _re
        m = _re.search(r'name="phone" value="([^"]*)"', verify_page.text)
        assert m, "verify page should carry the phone in a hidden field"
        phone_from_page = m.group(1)
        assert phone_from_page == STAFF_PHONE, (
            f"redirect corrupted the phone id: got {phone_from_page!r}"
        )
        code = _otp_code(STAFF_PHONE)
        final = client.post(
            "/dashboard/verify", data={"phone": phone_from_page, "code": code}, follow_redirects=False,
        )
        assert final.status_code == 303
        assert final.headers["location"] == "/dashboard/queue"

    def test_otp_resend_is_rate_limited(self, client, store_id):
        r1 = client.post("/dashboard/login", data={"phone": STAFF_PHONE_RAW})
        r2 = client.post("/dashboard/login", data={"phone": STAFF_PHONE_RAW})
        assert r1.status_code == 303 or r1.status_code == 200
        assert "wait" in r2.text.lower()

    def test_single_store_staff_goes_straight_to_queue(self, client, store_id):
        _login(client, store_id)
        r = client.get("/dashboard/queue")
        assert r.status_code == 200
        assert "Dashboard Test Cafe" in r.text

    def test_owner_role_still_displays_as_staff(self, client, store_id):
        """The dashboard never surfaces the DB's owner/manager/staff role
        distinction to the person signed in -- STAFF_PHONE is seeded with
        role="owner" (see the store_id fixture), but the page must always
        say "(staff)", never "(owner)"."""
        _login(client, store_id)
        r = client.get("/dashboard/queue")
        assert "Signed in (staff)" in r.text
        assert "(owner)" not in r.text

    def test_multi_store_staff_is_asked_to_choose(self, client, store_id):
        chain_id = seed_chain("Second Chain")
        store_b = seed_store(chain_id, name="Second Cafe")
        seed_member(store_b, STAFF_PHONE, role="owner")

        client.post("/dashboard/login", data={"phone": STAFF_PHONE_RAW})
        code = _otp_code(STAFF_PHONE)
        r = client.post("/dashboard/verify", data={"phone": STAFF_PHONE, "code": code}, follow_redirects=False)
        assert r.headers["location"] == "/dashboard/select-store"

        choose = client.get("/dashboard/select-store")
        assert "Dashboard Test Cafe" in choose.text and "Second Cafe" in choose.text

        picked = client.post("/dashboard/select-store", data={"store_id": store_b}, follow_redirects=False)
        assert picked.status_code == 303
        assert picked.headers["location"] == "/dashboard/queue"
        queue = client.get("/dashboard/queue")
        assert "Second Cafe" in queue.text

    def test_cannot_select_a_store_not_staffed_for(self, client, store_id):
        other_chain = seed_chain("Unrelated Chain")
        other_store = seed_store(other_chain, name="Not Yours")

        client.post("/dashboard/login", data={"phone": STAFF_PHONE_RAW})
        code = _otp_code(STAFF_PHONE)
        client.post("/dashboard/verify", data={"phone": STAFF_PHONE, "code": code})
        r = client.post("/dashboard/select-store", data={"store_id": other_store}, follow_redirects=False)
        assert r.headers["location"] == "/dashboard/select-store"  # bounced back, not accepted

    def test_logout_clears_the_session(self, client, store_id):
        _login(client, store_id)
        client.get("/dashboard/logout")
        r = client.get("/dashboard/queue", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/dashboard/login"


# ── queue actions parity with the WhatsApp staff commands ──────────────────

class TestDashboardQueueActions:
    def test_queue_page_shows_waiting_guests(self, client, store_id):
        from app.agents.maitre_d.agent import get_maitre_d
        get_maitre_d(store_id).handle_message("+923005551111", "table for 2, it's Ahmed")
        _login(client, store_id)
        r = client.get("/dashboard/queue")
        assert "Ahmed" in r.text
        assert "1" in r.text  # queue number

    def test_admit_button_pops_the_front_of_the_queue(self, client, store_id):
        from app.agents.maitre_d.agent import get_maitre_d
        from app.agents.maitre_d.store import Store
        get_maitre_d(store_id).handle_message("+923005551111", "table for 2, it's Aman")
        get_maitre_d(store_id).handle_message("+923005552222", "table for 2, it's Bilal")
        _login(client, store_id)

        client.post("/dashboard/queue/admit", data={"branch_name": ""})
        remaining = Store(store_id).list_queue(status="waiting")
        assert [e.name for e in remaining] == ["Bilal"]
        assert remaining[0].position == 1

    def test_remove_button_takes_someone_out(self, client, store_id):
        from app.agents.maitre_d.agent import get_maitre_d
        from app.agents.maitre_d.store import Store
        get_maitre_d(store_id).handle_message("+923005551111", "table for 2, it's Aman")
        get_maitre_d(store_id).handle_message("+923005552222", "table for 2, it's Bilal")
        _login(client, store_id)

        client.post("/dashboard/queue/remove", data={"position": 1, "branch_name": ""})
        remaining = Store(store_id).list_queue(status="waiting")
        assert [e.name for e in remaining] == ["Bilal"]

    def test_insert_adds_a_walk_in_at_a_position(self, client, store_id):
        from app.agents.maitre_d.agent import get_maitre_d
        from app.agents.maitre_d.store import Store
        get_maitre_d(store_id).handle_message("+923005551111", "table for 2, it's Aman")
        _login(client, store_id)

        client.post("/dashboard/queue/insert", data={
            "position": 1, "name": "Walked In", "party_size": 3, "phone": "", "branch_name": "",
        })
        ordered = Store(store_id).list_queue(status="waiting")
        assert [e.name for e in ordered] == ["Walked In", "Aman"]
        assert ordered[0].phone == ""

    def test_queue_fragment_endpoint_is_what_the_page_polls(self, client, store_id):
        from app.agents.maitre_d.agent import get_maitre_d
        get_maitre_d(store_id).handle_message("+923005551111", "table for 2, it's Ahmed")
        _login(client, store_id)
        r = client.get("/dashboard/queue/fragment")
        assert "Ahmed" in r.text
        assert "<html" not in r.text.lower()  # a fragment, not a full page

    def test_guest_name_is_html_escaped(self, client, store_id):
        """A guest can put anything in the "name" slot -- Jinja2's default
        autoescaping must stop it becoming live HTML in a staff member's
        browser."""
        from app.agents.maitre_d.agent import get_maitre_d
        get_maitre_d(store_id).handle_message(
            "+923005551111", "table for 2, it's Xavier",
        )
        # Directly force a dangerous name into the entry to test rendering,
        # since the NLU name-extraction regex wouldn't pass through raw
        # markup anyway -- this simulates a maliciously crafted request
        # reaching the DB by some other path.
        from app.agents.maitre_d.store import Store
        entry = Store(store_id).list_queue(status="waiting")[0]
        from app.core.db import SessionLocal, MaitreDQueueEntry
        with SessionLocal() as db:
            row = db.query(MaitreDQueueEntry).filter(MaitreDQueueEntry.id == entry.id).first()
            row.name = "<script>alert(1)</script>"
            db.commit()

        _login(client, store_id)
        r = client.get("/dashboard/queue")
        assert "<script>alert(1)</script>" not in r.text
        assert "&lt;script&gt;" in r.text


# ── VIPs ─────────────────────────────────────────────────────────────────────

class TestDashboardVips:
    def test_add_vip_via_form(self, client, store_id):
        _login(client, store_id)
        client.post("/dashboard/vips/add", data={
            "phone": "+923001112222", "name": "Ayesha Khan", "notes": "food critic",
        })
        from app.agents.maitre_d.config import VenueConfig
        cfg = VenueConfig.load(store_id)
        assert cfg.vip_for("+923001112222") is not None
        r = client.get("/dashboard/vips")
        assert "Ayesha Khan" in r.text


# ── Settings ─────────────────────────────────────────────────────────────────

class TestDashboardSettings:
    def test_toggle_booking_off_and_on(self, client, store_id):
        from app.agents.maitre_d.config import is_booking_enabled
        _login(client, store_id)
        assert is_booking_enabled(store_id) is True

        client.post("/dashboard/settings/booking", data={"enabled": "off"})
        assert is_booking_enabled(store_id) is False

        client.post("/dashboard/settings/booking", data={"enabled": "on"})
        assert is_booking_enabled(store_id) is True

    def test_set_thresholds(self, client, store_id):
        from app.agents.maitre_d.config import get_seated_grace_minutes, get_queue_stale_minutes
        _login(client, store_id)
        client.post("/dashboard/settings/thresholds", data={
            "seated_grace_minutes": 30, "queue_stale_minutes": 45,
        })
        assert get_seated_grace_minutes(store_id) == 30
        assert get_queue_stale_minutes(store_id) == 45

    def test_out_of_range_threshold_is_rejected(self, client, store_id):
        from app.agents.maitre_d.config import get_queue_stale_minutes
        _login(client, store_id)
        r = client.post("/dashboard/settings/thresholds", data={
            "seated_grace_minutes": 60, "queue_stale_minutes": 0,
        })
        assert r.status_code == 400
        assert "between" in r.text.lower()
        assert get_queue_stale_minutes(store_id) == 90  # unchanged


# ── Store isolation ──────────────────────────────────────────────────────────

class TestDashboardStoreIsolation:
    def test_actions_never_touch_another_store(self, client, store_id):
        other_chain = seed_chain("Other Chain")
        other_store = seed_store(other_chain, name="Other Cafe")

        from app.agents.maitre_d.agent import get_maitre_d
        from app.agents.maitre_d.store import Store
        get_maitre_d(other_store).handle_message("+923009998888", "table for 2, it's Zara")

        _login(client, store_id)  # session is scoped to `store_id`, not `other_store`
        client.post("/dashboard/queue/admit", data={"branch_name": ""})

        # Nothing in the OTHER store's queue was touched.
        other_queue = Store(other_store).list_queue(status="waiting")
        assert [e.name for e in other_queue] == ["Zara"]


# ── Per-branch access: owner sees everything, staff/manager see only their
# assigned branch ────────────────────────────────────────────────────────────

def _login_as(client, whatsapp_id: str) -> None:
    """Same as _login, but for an arbitrary already-registered phone --
    used to sign in as a branch-scoped staff/manager member instead of
    the store fixture's default owner."""
    from urllib.parse import quote
    raw = whatsapp_id.removeprefix("whatsapp:")
    client.post("/dashboard/login", data={"phone": raw})
    code = _otp_code(whatsapp_id)
    r = client.post("/dashboard/verify", data={"phone": whatsapp_id, "code": code}, follow_redirects=False)
    assert r.status_code == 303


def _seed_two_bookable_branches(store_id: int) -> tuple[int, int]:
    from app.core.db import SessionLocal, MaitreDLocation
    with SessionLocal() as db:
        blue = MaitreDLocation(
            store_id=store_id, branch_key="new_blue_area", name="New Blue Area",
            is_primary=True, accepts_reservations=True,
        )
        bahria = MaitreDLocation(
            store_id=store_id, branch_key="bahria_town", name="Bahria Town",
            accepts_reservations=True,
        )
        db.add_all([blue, bahria])
        db.commit()
        db.refresh(blue)
        db.refresh(bahria)
        return blue.id, bahria.id


class TestDashboardBranchScoping:
    def test_owner_sees_a_tab_per_branch(self, client, store_id):
        _seed_two_bookable_branches(store_id)
        _login(client, store_id)
        r = client.get("/dashboard/queue")
        assert "New Blue Area" in r.text
        assert "Bahria Town" in r.text

    def test_owner_settings_page_lists_every_branch_independently(self, client, store_id):
        _seed_two_bookable_branches(store_id)
        _login(client, store_id)
        r = client.get("/dashboard/settings")
        assert "New Blue Area" in r.text
        assert "Bahria Town" in r.text

    def test_unassigned_staff_member_is_blocked_with_an_explanation(self, client, store_id):
        _seed_two_bookable_branches(store_id)
        staff = "whatsapp:+923221111111"
        seed_member(store_id, staff, role="staff")  # no location assigned
        _login_as(client, staff)
        r = client.get("/dashboard/queue")
        assert r.status_code == 403
        assert "haven't been assigned" in r.text.lower()

    def test_assigned_staff_member_only_sees_their_branch(self, client, store_id):
        from app.agents.maitre_d.agent import get_maitre_d
        blue_id, bahria_id = _seed_two_bookable_branches(store_id)
        get_maitre_d(store_id).handle_message("+923005551111", "table for 2 at New Blue Area, it's Ahmed")
        get_maitre_d(store_id).handle_message("+923005552222", "table for 2 at Bahria Town, it's Sara")

        staff = "whatsapp:+923221111111"
        seed_member(store_id, staff, role="staff", location_id=blue_id)
        _login_as(client, staff)
        r = client.get("/dashboard/queue")
        assert "Ahmed" in r.text
        assert "Sara" not in r.text
        # No branch tabs for a scoped member -- there's only one branch to see.
        assert "Bahria Town" not in r.text

    def test_assigned_staff_admit_ignores_a_spoofed_branch_name_field(self, client, store_id):
        from app.agents.maitre_d.agent import get_maitre_d
        from app.agents.maitre_d.store import Store
        blue_id, bahria_id = _seed_two_bookable_branches(store_id)
        get_maitre_d(store_id).handle_message("+923005551111", "table for 2 at New Blue Area, it's Ahmed")
        get_maitre_d(store_id).handle_message("+923005552222", "table for 2 at Bahria Town, it's Sara")

        staff = "whatsapp:+923221111111"
        seed_member(store_id, staff, role="staff", location_id=blue_id)
        _login_as(client, staff)
        # A tampered form claiming "Bahria Town" must be ignored -- their
        # own assignment (New Blue Area) always wins.
        client.post("/dashboard/queue/admit", data={"branch_name": "Bahria Town"})

        remaining_bahria = Store(store_id).list_queue(status="waiting", location_id=bahria_id)
        assert [e.name for e in remaining_bahria] == ["Sara"]  # untouched
        remaining_blue = Store(store_id).list_queue(status="waiting", location_id=blue_id)
        assert remaining_blue == []  # Ahmed was admitted instead

    def test_owner_can_assign_a_staff_member_from_the_staff_page(self, client, store_id):
        from app.core.db import SessionLocal, StoreMember
        blue_id, bahria_id = _seed_two_bookable_branches(store_id)
        staff = "whatsapp:+923221111111"
        seed_member(store_id, staff, role="staff")
        _login(client, store_id)

        page = client.get("/dashboard/staff")
        assert staff in page.text

        with SessionLocal() as db:
            member = db.query(StoreMember).filter(
                StoreMember.store_id == store_id, StoreMember.whatsapp == staff,
            ).first()
            member_id = member.id

        client.post("/dashboard/staff/assign", data={"member_id": member_id, "location_id": bahria_id})

        with SessionLocal() as db:
            member = db.query(StoreMember).filter(
                StoreMember.store_id == store_id, StoreMember.whatsapp == staff,
            ).first()
            assert member.location_id == bahria_id

    def test_non_owner_cannot_reach_the_staff_page(self, client, store_id):
        blue_id, bahria_id = _seed_two_bookable_branches(store_id)
        staff = "whatsapp:+923221111111"
        seed_member(store_id, staff, role="staff", location_id=blue_id)
        _login_as(client, staff)
        r = client.get("/dashboard/staff")
        assert r.status_code == 403

    def test_manager_is_scoped_like_staff(self, client, store_id):
        from app.agents.maitre_d.agent import get_maitre_d
        blue_id, bahria_id = _seed_two_bookable_branches(store_id)
        get_maitre_d(store_id).handle_message("+923005551111", "table for 2 at Bahria Town, it's Ahmed")

        manager = "whatsapp:+923223333333"
        seed_member(store_id, manager, role="manager", location_id=bahria_id)
        _login_as(client, manager)
        r = client.get("/dashboard/queue")
        assert "Ahmed" in r.text

    def test_single_location_store_shows_no_tabs_and_needs_no_assignment(self, client, store_id):
        from app.agents.maitre_d.agent import get_maitre_d
        get_maitre_d(store_id).handle_message("+923005551111", "table for 2, it's Ahmed")
        staff = "whatsapp:+923221111111"
        seed_member(store_id, staff, role="staff")  # no location, none needed
        _login_as(client, staff)
        r = client.get("/dashboard/queue")
        assert r.status_code == 200
        assert "Ahmed" in r.text
