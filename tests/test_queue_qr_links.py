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
    def test_no_provider_configured_404s(self, client, store_id):
        r = client.get(f"/admin/stores/{store_id}/queue-links")
        assert r.status_code == 404

    def test_single_location_store_gets_one_plain_link(self, client, store_id):
        _seed_meta_number(store_id)
        r = client.get(f"/admin/stores/{store_id}/queue-links")
        assert r.status_code == 200
        data = r.json()
        assert len(data["links"]) == 1
        link = data["links"][0]
        assert link["branch_key"] is None
        assert link["text"] == "Join the Queue"
        assert "15551590482" in link["url"]
        assert unquote(link["url"].split("text=")[1]) == "Join the Queue"

    def test_multi_location_store_gets_one_link_per_bookable_branch(self, client, store_id):
        _seed_meta_number(store_id)
        _seed_two_locations(store_id)
        r = client.get(f"/admin/stores/{store_id}/queue-links")
        assert r.status_code == 200
        links = r.json()["links"]
        # F-8/2 is delivery-only -- must not get its own queue-join QR link.
        assert len(links) == 1
        assert links[0]["branch_key"] == "new_blue_area"
        assert unquote(links[0]["url"].split("text=")[1]) == "Join the Queue - new_blue_area"


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
