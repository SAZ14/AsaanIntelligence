"""Tests for Twilio WhatsApp dispatch and permanent venue QR join flow."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.ingest.loader import load_customers, load_loyalty_rules
from app.models.canonical import LoyaltyCustomer
from app.services.guest import join_guest, load_venues
from app.services.messaging import (
    ConsoleMessageDispatcher,
    TwilioWhatsAppDispatcher,
    get_dispatcher,
    normalize_phone,
)

DATA = Path(__file__).resolve().parent.parent / "data"


def test_normalize_phone_pk():
    assert normalize_phone("03001234567") == "+923001234567"
    assert normalize_phone("+923001234567") == "+923001234567"


def test_get_dispatcher_console_without_twilio():
    assert isinstance(get_dispatcher(use_twilio=False), ConsoleMessageDispatcher)


def test_twilio_dispatcher_send_success():
    from app.agents.customer import CustomerIncentive

    incentive = CustomerIncentive(
        customer_ref="C_TEST",
        display_name="Test",
        incentive_type="winback_lapsed",
        discount_pct=15,
        reward_text="15% off",
        message="Hi Test",
        channel="whatsapp",
        phone="+923001234567",
        priority=1,
        trigger_reason="test",
    )
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(sid="SM123")

    with patch("twilio.rest.Client", return_value=mock_client):
        disp = TwilioWhatsAppDispatcher(
            account_sid="ACtest", auth_token="token", from_number="whatsapp:+14155238886",
        )
        result = disp.send(incentive)

    assert result.status == "sent"
    assert result.provider_id == "SM123"
    mock_client.messages.create.assert_called_once()


def test_join_guest_new():
    venues = load_venues(DATA / "venues.json")
    venue = venues["sugar-rush"]
    registry: dict[str, LoyaltyCustomer] = {}
    rules = load_loyalty_rules(DATA / "loyalty_rules.json")

    result = join_guest(
        venue, registry, "New Guest", "03009998877", [], rules,
    )
    assert not result.is_returning
    assert result.customer_ref.startswith("C")
    assert result.short_code == result.customer_ref[-4:].upper()
    assert len(registry) == 1


def test_join_guest_returning():
    venues = load_venues(DATA / "venues.json")
    venue = venues["sugar-rush"]
    rules = load_loyalty_rules(DATA / "loyalty_rules.json")
    registry = {
        "Cabc123": LoyaltyCustomer(
            customer_ref="Cabc123", qr_token="QR-x", display_name="Old Name",
            phone="+923001111111", channel="whatsapp",
        ),
    }
    result = join_guest(
        venue, registry, "Updated Name", "+923001111111", [], rules,
    )
    assert result.is_returning
    assert result.display_name == "Updated Name"
    assert len(registry) == 1


@pytest.fixture
def guest_client(tmp_path, monkeypatch):
    import shutil
    for name in ("sales_detail.csv", "menu.csv", "staff.csv", "customers.csv",
                 "loyalty_rules.json", "venues.json"):
        shutil.copy(DATA / name, tmp_path / name)
    monkeypatch.setenv("ASAAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ASAAN_OUTBOX_DIR", str(tmp_path / "output"))
    return TestClient(app)


def test_join_page_renders(guest_client):
    r = guest_client.get("/join/sugar-rush")
    assert r.status_code == 200
    assert "Sugar Rush" in r.text
    assert "WhatsApp" in r.text


def test_qr_join_api(guest_client, tmp_path):
    r = guest_client.post("/qr/join", json={
        "venue_slug": "sugar-rush",
        "display_name": "API Guest",
        "phone": "+923009998877",
        "channel": "whatsapp",
        "opted_in": True,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["customer_ref"].startswith("C")
    assert data["short_code"]

    registry = load_customers(tmp_path / "customers.csv")
    assert data["customer_ref"] in registry
    assert registry[data["customer_ref"]].display_name == "API Guest"


def test_qr_recognize_api(guest_client):
    guest_client.post("/qr/join", json={
        "venue_slug": "sugar-rush",
        "display_name": "Recognize Me",
        "phone": "+923008887766",
        "channel": "whatsapp",
    })
    r = guest_client.get("/qr/recognize", params={
        "phone": "+923008887766",
        "venue_slug": "sugar-rush",
    })
    assert r.status_code == 200
    assert r.json()["is_returning"] is True
