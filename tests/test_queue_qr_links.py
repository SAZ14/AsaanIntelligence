"""wa.me link generation for the entrance/booking-area QR code(s) --
GET /admin/stores/{id}/queue-links -- and the branch-code trigger it
depends on (gateway/customer.py's _is_booking_trigger prefix match).
"""
from __future__ import annotations

from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from tests.conftest import seed_chain, seed_store


@pytest.fixture
def client():
    from app.gateway.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def store_id():
    chain_id = seed_chain("QR Links Chain")
    return seed_store(chain_id, name="QR Links Cafe", location="F-7, Islamabad")


def _seed_meta_number(store_id: int, display_number: str = "+1 555-159-0482") -> None:
    from app.core.db import SessionLocal, StoreMetaNumber
    with SessionLocal() as db:
        db.add(StoreMetaNumber(
            store_id=store_id, phone_number_id="123456", waba_id="789",
            access_token="test-token", display_number=display_number,
        ))
        db.commit()


def _seed_two_locations(store_id: int) -> None:
    from app.core.db import SessionLocal, MaitreDLocation
    with SessionLocal() as db:
        db.add_all([
            MaitreDLocation(
                store_id=store_id, branch_key="new_blue_area", name="New Blue Area",
                is_primary=True, accepts_reservations=True,
            ),
            MaitreDLocation(
                store_id=store_id, branch_key="f82", name="F-8/2 Madina Market",
                is_primary=False, accepts_reservations=False,
            ),
        ])
        db.commit()


class TestQueueLinksEndpoint:
    """These URLs are what actually gets printed on the QR code -- they
    must be STATIC (the relinker's own URL), never a direct wa.me link,
    since the whole point of the relinker is that what's inside the code
    changes on every scan while the printed sticker never has to."""

    def test_no_provider_configured_404s(self, client, store_id):
        r = client.get(f"/admin/stores/{store_id}/queue-links")
        assert r.status_code == 404

    def test_single_location_store_gets_one_relinker_link(self, client, store_id):
        _seed_meta_number(store_id)
        r = client.get(f"/admin/stores/{store_id}/queue-links")
        assert r.status_code == 200
        data = r.json()
        assert len(data["links"]) == 1
        link = data["links"][0]
        assert link["branch_key"] is None
        assert link["url"].endswith(f"/q/{store_id}")

    def test_multi_location_store_gets_one_link_per_bookable_branch(self, client, store_id):
        _seed_meta_number(store_id)
        _seed_two_locations(store_id)
        r = client.get(f"/admin/stores/{store_id}/queue-links")
        assert r.status_code == 200
        links = r.json()["links"]
        # F-8/2 is delivery-only -- must not get its own queue-join QR link.
        assert len(links) == 1
        assert links[0]["branch_key"] == "new_blue_area"
        assert links[0]["url"].endswith(f"/q/{store_id}?branch=new_blue_area")


class TestEntranceRelinker:
    """GET /q/{store_id} -- the ONLY URL a printed QR code ever encodes.
    Mints a fresh one-time code on every hit and 302s straight into
    WhatsApp with it embedded. The code's DB row carries the branch
    (and the store, via a store-scoped lookup) -- the wa.me message
    text itself never names either (see gateway/customer.py's
    _peek_entrance_code/_burn_entrance_code for the other half)."""

    def test_no_provider_configured_404s(self, client, store_id):
        r = client.get(f"/q/{store_id}", follow_redirects=False)
        assert r.status_code == 404

    def test_redirects_to_whatsapp_with_a_code_embedded(self, client, store_id):
        _seed_meta_number(store_id)
        r = client.get(f"/q/{store_id}", follow_redirects=False)
        assert r.status_code == 302
        location = unquote(r.headers["location"])
        assert "wa.me/15551590482" in location
        assert "Join the Queue - " in location
        code = location.rsplit("-", 1)[1].strip()
        assert len(code) >= 6 and code.isalnum()

    def test_two_scans_mint_two_different_codes(self, client, store_id):
        _seed_meta_number(store_id)
        loc1 = unquote(client.get(f"/q/{store_id}", follow_redirects=False).headers["location"])
        loc2 = unquote(client.get(f"/q/{store_id}", follow_redirects=False).headers["location"])
        code1 = loc1.rsplit("-", 1)[1].strip()
        code2 = loc2.rsplit("-", 1)[1].strip()
        assert code1 != code2

    def test_branch_is_resolved_from_the_code_not_the_message_text(self, client, store_id):
        """The prefilled message never names the branch anymore -- the
        code's own DB row carries it, so a scan at the New Blue Area
        entrance must redeem to New Blue Area's location_id even though
        the wa.me text just says "Join the Queue - <code>"."""
        _seed_meta_number(store_id)
        _seed_two_locations(store_id)
        from app.core.db import SessionLocal, MaitreDLocation
        with SessionLocal() as db:
            blue_id = db.query(MaitreDLocation.id).filter(
                MaitreDLocation.store_id == store_id,
                MaitreDLocation.branch_key == "new_blue_area",
            ).scalar()

        r = client.get(f"/q/{store_id}?branch=new_blue_area", follow_redirects=False)
        assert r.status_code == 302
        location = unquote(r.headers["location"])
        assert "new_blue_area" not in location
        code = location.rsplit("-", 1)[1].strip()

        from app.agents.maitre_d.store import Store
        ok, resolved_location_id = Store(store_id).redeem_entrance_code(code)
        assert ok is True
        assert resolved_location_id == blue_id

    def test_unknown_branch_404s(self, client, store_id):
        _seed_meta_number(store_id)
        _seed_two_locations(store_id)
        r = client.get(f"/q/{store_id}?branch=nonexistent", follow_redirects=False)
        assert r.status_code == 404


class TestEntranceCodeRedemption:
    """Store.generate_entrance_code / redeem_entrance_code -- the part
    that makes a stale screenshot or memorised trigger message stop
    working (gateway/customer.py's _peek_entrance_code/_burn_entrance_code
    call straight through to these)."""

    def test_fresh_code_redeems_once(self, store_id):
        from app.agents.maitre_d.store import Store
        store = Store(store_id)
        code = store.generate_entrance_code(None)
        ok, location_id = store.redeem_entrance_code(code)
        assert ok is True
        assert location_id == 0

    def test_same_code_cannot_be_redeemed_twice(self, store_id):
        from app.agents.maitre_d.store import Store
        store = Store(store_id)
        code = store.generate_entrance_code(None)
        assert store.redeem_entrance_code(code) == (True, 0)
        assert store.redeem_entrance_code(code) == (False, 0)

    def test_unknown_code_is_rejected(self, store_id):
        from app.agents.maitre_d.store import Store
        assert Store(store_id).redeem_entrance_code("NOSUCHCODE") == (False, 0)

    def test_code_minted_for_a_different_store_does_not_redeem(self, store_id):
        from app.agents.maitre_d.store import Store
        other_chain = seed_chain("Other QR Chain")
        other_store = seed_store(other_chain, name="Other Store")
        code = Store(other_store).generate_entrance_code(None)
        assert Store(store_id).redeem_entrance_code(code) == (False, 0)

    def test_expired_code_is_rejected(self, store_id):
        from datetime import datetime, timedelta
        from app.core.db import SessionLocal, MaitreDEntranceCode
        from app.agents.maitre_d.store import Store
        from app.agents.maitre_d.config import set_entrance_code_ttl_minutes

        set_entrance_code_ttl_minutes(store_id, 15)
        code = Store(store_id).generate_entrance_code(None)
        with SessionLocal() as db:
            row = db.query(MaitreDEntranceCode).filter(MaitreDEntranceCode.code == code).first()
            row.created_at = datetime.utcnow() - timedelta(minutes=16)
            db.commit()
        assert Store(store_id).redeem_entrance_code(code) == (False, 0)

    def test_code_records_the_location_it_was_minted_for(self, store_id):
        from app.agents.maitre_d.store import Store
        ok, location_id = Store(store_id).redeem_entrance_code(
            Store(store_id).generate_entrance_code(42)
        )
        assert ok is True
        assert location_id == 42


class TestBranchCodeTriggerRegression:
    """The trigger-prefix change (_is_booking_trigger) that makes the
    branch-coded links above actually work, verified in isolation."""

    def test_plain_trigger_still_matches(self):
        from app.gateway.customer import _is_booking_trigger
        assert _is_booking_trigger("Join the Queue") is True
        assert _is_booking_trigger("\U0001f3ab Join the Queue!") is True

    def test_branch_coded_trigger_matches(self):
        from app.gateway.customer import _is_booking_trigger
        assert _is_booking_trigger("Join the Queue - F7") is True
        assert _is_booking_trigger("Join the Queue - new_blue_area") is True

    def test_phrase_mid_sentence_does_not_match(self):
        from app.gateway.customer import _is_booking_trigger
        assert _is_booking_trigger("please join the queue for me") is False
        assert _is_booking_trigger("I want to join the queue") is False

    def test_unrelated_text_does_not_match(self):
        from app.gateway.customer import _is_booking_trigger
        assert _is_booking_trigger("hi, table for 2") is False
        assert _is_booking_trigger("hello") is False
