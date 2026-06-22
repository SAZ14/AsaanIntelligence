"""Tests for Twilio inbound webhook signature validation."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app.api.main import app

DATA = Path(__file__).resolve().parent.parent / "data"
WEBHOOK_PATH = "/webhooks/twilio/whatsapp"
PUBLIC_BASE = "http://testserver"
AUTH_TOKEN = "test_auth_token"


@pytest.fixture
def secure_client(tmp_path, monkeypatch):
    for name in ("sales_detail.csv", "menu.csv", "staff.csv", "customers.csv",
                 "loyalty_rules.json", "venues.json"):
        shutil.copy(DATA / name, tmp_path / name)
    monkeypatch.setenv("ASAAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ASAAN_OUTBOX_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("ASAAN_PUBLIC_BASE_URL", PUBLIC_BASE)
    monkeypatch.setenv("ASAAN_VALIDATE_TWILIO_SIGNATURE", "1")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    return TestClient(app)


def _signed_post(client, params):
    sig = RequestValidator(AUTH_TOKEN).compute_signature(
        PUBLIC_BASE + WEBHOOK_PATH, params,
    )
    return client.post(WEBHOOK_PATH, data=params, headers={"X-Twilio-Signature": sig})


def test_valid_signature_accepted(secure_client):
    params = {"From": "whatsapp:+923005556666", "Body": "Hi Sugar Rush!"}
    with patch("app.services.whatsapp_agent.send_whatsapp_text"):
        r = _signed_post(secure_client, params)
    assert r.status_code == 200
    assert "Response" in r.text


def test_bad_signature_rejected(secure_client):
    params = {"From": "whatsapp:+923005556666", "Body": "Hi Sugar Rush!"}
    with patch("app.services.whatsapp_agent.send_whatsapp_text") as mock_send:
        r = secure_client.post(
            WEBHOOK_PATH, data=params,
            headers={"X-Twilio-Signature": "obviously-wrong"},
        )
    assert r.status_code == 403
    mock_send.assert_not_called()


def test_tampered_body_rejected(secure_client):
    # Sign one body, then submit a different one — signature no longer matches.
    sig = RequestValidator(AUTH_TOKEN).compute_signature(
        PUBLIC_BASE + WEBHOOK_PATH, {"From": "whatsapp:+923005556666", "Body": "original"},
    )
    with patch("app.services.whatsapp_agent.send_whatsapp_text"):
        r = secure_client.post(
            WEBHOOK_PATH,
            data={"From": "whatsapp:+923005556666", "Body": "tampered"},
            headers={"X-Twilio-Signature": sig},
        )
    assert r.status_code == 403


def test_validation_disabled_by_default(tmp_path, monkeypatch):
    for name in ("sales_detail.csv", "menu.csv", "staff.csv", "customers.csv",
                 "loyalty_rules.json", "venues.json"):
        shutil.copy(DATA / name, tmp_path / name)
    monkeypatch.setenv("ASAAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ASAAN_OUTBOX_DIR", str(tmp_path / "output"))
    monkeypatch.delenv("ASAAN_VALIDATE_TWILIO_SIGNATURE", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    client = TestClient(app)
    with patch("app.services.whatsapp_agent.send_whatsapp_text"):
        r = client.post(
            WEBHOOK_PATH,
            data={"From": "whatsapp:+923005556666", "Body": "Hi"},
        )
    assert r.status_code == 200
