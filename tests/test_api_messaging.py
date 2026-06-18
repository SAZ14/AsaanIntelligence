"""Tests for message dispatch and FastAPI QR endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.customer import CustomerIncentive
from app.api.main import app
from app.ingest.loader import load_customers, save_customers
from app.models.canonical import LoyaltyCustomer
from app.services.messaging import ConsoleMessageDispatcher, dispatch_incentives

DATA = Path(__file__).resolve().parent.parent / "data"


def _incentive(phone: str = "+923001234567") -> CustomerIncentive:
    return CustomerIncentive(
        customer_ref="C_TEST",
        display_name="Test Guest",
        incentive_type="visit_milestone",
        discount_pct=10,
        reward_text="10% off",
        message="Hi Test, enjoy 10% off!",
        channel="sms",
        phone=phone,
        priority=1,
        trigger_reason="5th visit",
    )


def test_dispatch_sends_with_phone(capsys):
    result = dispatch_incentives([_incentive()], ConsoleMessageDispatcher(), limit=1)
    assert len(result.sent) == 1
    assert result.sent[0].status == "sent"
    captured = capsys.readouterr()
    assert "10% off" in captured.out or "Test Guest" in captured.out


def test_dispatch_skips_without_phone():
    result = dispatch_incentives([_incentive(phone="")], ConsoleMessageDispatcher())
    assert len(result.sent) == 0
    assert len(result.skipped) == 1


def test_save_and_load_customers(tmp_path):
    path = tmp_path / "customers.csv"
    registry = {
        "C001": LoyaltyCustomer(
            customer_ref="C001", qr_token="QR-001",
            display_name="Ali", phone="+923001111111", channel="whatsapp",
        ),
    }
    save_customers(path, registry)
    loaded = load_customers(path)
    assert loaded["C001"].display_name == "Ali"
    assert loaded["C001"].qr_token == "QR-001"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import shutil
    for name in ("sales_detail.csv", "menu.csv", "staff.csv", "customers.csv"):
        shutil.copy(DATA / name, tmp_path / name)
    monkeypatch.setenv("ASAAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ASAAN_OUTBOX_DIR", str(tmp_path / "output"))
    return TestClient(app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_qr_scan_persists(client, tmp_path):
    r = client.post("/qr/scan", json={
        "qr_token": "QR-NEWTEST",
        "customer_ref": "C_NEW_GUEST",
        "display_name": "New Guest",
        "phone": "+923009999999",
        "channel": "whatsapp",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["customer_ref"] == "C_NEW_GUEST"
    assert data["registered"] is True

    reg_path = tmp_path / "customers.csv"
    loaded = load_customers(reg_path)
    assert "C_NEW_GUEST" in loaded
    assert loaded["C_NEW_GUEST"].phone == "+923009999999"


def test_qr_lookup(client):
    r = client.get("/qr/lookup/QR-a1b2c3d4")
    assert r.status_code == 200
    assert r.json()["display_name"] == "Ayesha Khan"


def test_customer_report_summary(client):
    r = client.get("/customer/report/summary")
    assert r.status_code == 200
    data = r.json()
    assert "tagline" in data
    assert data["loyalty"]["total_members"] > 0
