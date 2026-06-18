"""Tests for WhatsApp-first guest onboarding agent."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.ingest.loader import load_customers, load_loyalty_rules
from app.models.canonical import LoyaltyCustomer
from app.services.guest import load_venues
from app.services.whatsapp_agent import (
    build_whatsapp_qr_url,
    handle_incoming_whatsapp,
    process_and_reply,
)

DATA = Path(__file__).resolve().parent.parent / "data"


def test_build_whatsapp_qr_url():
    url = build_whatsapp_qr_url(
        "+14155238886",
        "Sugar Rush",
        greeting="Hi Sugar Rush!",
    )
    assert url.startswith("https://wa.me/14155238886?text=")
    assert "Sugar" in url


def test_agent_asks_for_name_on_first_message(tmp_path):
    venue = load_venues(DATA / "venues.json")["sugar-rush"]
    rules = load_loyalty_rules(DATA / "loyalty_rules.json")
    registry: dict[str, LoyaltyCustomer] = {}
    sessions = tmp_path / "sessions.json"
    reg_path = tmp_path / "customers.csv"

    reply = handle_incoming_whatsapp(
        "whatsapp:+923009991111",
        "Hi Sugar Rush! I'd like to join rewards.",
        venue=venue,
        registry=registry,
        orders=[],
        rules=rules,
        sessions_path=sessions,
        registry_path=reg_path,
    )
    assert "What name" in reply.body
    assert not reply.registered
    assert len(registry) == 0


def test_agent_registers_after_name(tmp_path):
    venue = load_venues(DATA / "venues.json")["sugar-rush"]
    rules = load_loyalty_rules(DATA / "loyalty_rules.json")
    registry: dict[str, LoyaltyCustomer] = {}
    sessions = tmp_path / "sessions.json"
    reg_path = tmp_path / "customers.csv"

    handle_incoming_whatsapp(
        "whatsapp:+923009992222",
        "Hello",
        venue=venue,
        registry=registry,
        orders=[],
        rules=rules,
        sessions_path=sessions,
        registry_path=reg_path,
    )
    reply = handle_incoming_whatsapp(
        "whatsapp:+923009992222",
        "Sara Ahmed",
        venue=venue,
        registry=registry,
        orders=[],
        rules=rules,
        sessions_path=sessions,
        registry_path=reg_path,
    )
    assert reply.registered
    assert "Sara Ahmed" in reply.body
    saved = load_customers(reg_path)
    assert len(saved) == 1
    assert "+923009992222" in next(iter(saved.values())).phone


def test_agent_welcomes_returning_guest(tmp_path):
    venue = load_venues(DATA / "venues.json")["sugar-rush"]
    rules = load_loyalty_rules(DATA / "loyalty_rules.json")
    registry = {
        "Cabc123": LoyaltyCustomer(
            customer_ref="Cabc123",
            qr_token="QR-x",
            display_name="Hamza",
            phone="+923003334444",
            channel="whatsapp",
        ),
    }
    sessions = tmp_path / "sessions.json"
    reg_path = tmp_path / "customers.csv"

    reply = handle_incoming_whatsapp(
        "whatsapp:+923003334444",
        "Hey",
        venue=venue,
        registry=registry,
        orders=[],
        rules=rules,
        sessions_path=sessions,
        registry_path=reg_path,
    )
    assert "Welcome back" in reply.body
    assert not reply.registered


@pytest.fixture
def webhook_client(tmp_path, monkeypatch):
    import shutil
    for name in ("sales_detail.csv", "menu.csv", "staff.csv", "customers.csv",
                 "loyalty_rules.json", "venues.json"):
        shutil.copy(DATA / name, tmp_path / name)
    monkeypatch.setenv("ASAAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ASAAN_OUTBOX_DIR", str(tmp_path / "output"))
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    return TestClient(app)


def test_twilio_webhook_asks_name(webhook_client):
    with patch("app.services.whatsapp_agent.send_whatsapp_text") as mock_send:
        r = webhook_client.post(
            "/webhooks/twilio/whatsapp",
            data={"From": "whatsapp:+923005556666", "Body": "Hi Sugar Rush!"},
        )
        assert r.status_code == 200
        assert "Response" in r.text
        mock_send.assert_called_once()
        assert "What name" in mock_send.call_args[0][1]


def test_twilio_webhook_full_signup(webhook_client, tmp_path):
    with patch("app.services.whatsapp_agent.send_whatsapp_text") as mock_send:
        webhook_client.post(
            "/webhooks/twilio/whatsapp",
            data={"From": "whatsapp:+923005557777", "Body": "Join"},
        )
        webhook_client.post(
            "/webhooks/twilio/whatsapp",
            data={"From": "whatsapp:+923005557777", "Body": "Ali Raza"},
        )
        assert mock_send.call_count == 2
        welcome = mock_send.call_args_list[1][0][1]
        assert "Ali Raza" in welcome
        registry = load_customers(tmp_path / "customers.csv")
        assert any(c.display_name == "Ali Raza" for c in registry.values())
