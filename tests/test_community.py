"""Comprehensive tests and evaluation for Sugar Rush community agents."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.agents.community_customer import handle_customer_message, process_customer_reply
from app.agents.community_merchant import handle_merchant_message, process_merchant_reply
from app.api.main import app
from app.community.models import CommunityMember, RedeemCode, StampEvent
from app.community.store import (
    append_redeem_code,
    append_stamp_event,
    load_members,
    load_redeem_codes,
    save_members,
)
from app.community.tokens import issue_redeem_code, is_redeem_code, validate_code
from app.community.models import VenueConfig
from app.jobs.leaderboard_broadcast import run_leaderboard_broadcast
from app.jobs.winback import run_winback

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture
def community_tmp(tmp_path, monkeypatch):
    for name in (
        "menu.csv", "sales_detail.csv", "staff.csv",
        "venue_config.json", "deals.json", "community_members.csv",
    ):
        src = DATA / name
        if src.exists():
            shutil.copy(src, tmp_path / name)
    (tmp_path / "redeem_codes.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "stamp_events.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "onboarding_sessions.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ASAAN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    paths = {
        "members_path": tmp_path / "community_members.csv",
        "redeem_path": tmp_path / "redeem_codes.jsonl",
        "events_path": tmp_path / "stamp_events.jsonl",
        "config_path": tmp_path / "venue_config.json",
        "deals_path": tmp_path / "deals.json",
        "menu_path": tmp_path / "menu.csv",
        "sessions_path": tmp_path / "onboarding_sessions.json",
        "sales_path": tmp_path / "sales_detail.csv",
        "staff_path": tmp_path / "staff.csv",
    }
    customer_paths = {k: paths[k] for k in (
        "members_path", "redeem_path", "events_path", "config_path",
        "deals_path", "menu_path", "sessions_path",
    )}
    merchant_paths = {k: paths[k] for k in (
        "config_path", "members_path", "events_path", "menu_path",
        "deals_path", "sales_path", "staff_path",
    )}
    return tmp_path, customer_paths, merchant_paths


def _enroll(customer_paths, phone="+923001111111", name="Sara Ahmed"):
    handle_customer_message(f"whatsapp:{phone}", "Join", **customer_paths)
    return handle_customer_message(f"whatsapp:{phone}", name, **customer_paths)


# ── Tokenization ──

def test_is_redeem_code_variants():
    assert is_redeem_code("SR-AB12")
    assert is_redeem_code("sr-ab12")
    assert not is_redeem_code("hello")
    assert not is_redeem_code("SR-ABC")
    assert not is_redeem_code("SR-AB123")


def test_redeem_code_unique(community_tmp):
    _, customer_paths, _ = community_tmp
    c1 = issue_redeem_code(customer_paths["redeem_path"], order_id="A")
    c2 = issue_redeem_code(customer_paths["redeem_path"], order_id="B")
    assert c1.code != c2.code


def test_expired_code_rejected(community_tmp):
    _, customer_paths, _ = community_tmp
    _enroll(customer_paths, "+923004444444", "Expired Test")
    old = RedeemCode(
        code="SR-OLD1",
        order_id="X",
        issued_at=(datetime.now(timezone.utc) - timedelta(days=60)).isoformat(),
    )
    append_redeem_code(customer_paths["redeem_path"], old)
    r = handle_customer_message("whatsapp:+923004444444", "SR-OLD1", **customer_paths)
    assert "expired" in r.body.lower()


def test_already_used_code_rejected(community_tmp):
    _, customer_paths, _ = community_tmp
    _enroll(customer_paths, "+923005555555", "Reuse Test")
    code = issue_redeem_code(customer_paths["redeem_path"])
    handle_customer_message("whatsapp:+923005555555", code.code, **customer_paths)
    r = handle_customer_message("whatsapp:+923005555555", code.code, **customer_paths)
    assert "already used" in r.body.lower()


def test_code_before_join_rejected(community_tmp):
    _, customer_paths, _ = community_tmp
    code = issue_redeem_code(customer_paths["redeem_path"])
    r = handle_customer_message("whatsapp:+923006666666", code.code, **customer_paths)
    assert "QR" in r.body or "join" in r.body.lower()


# ── Customer agent: onboarding ──

def test_qr_onboarding_asks_name_then_welcomes(community_tmp):
    _, customer_paths, _ = community_tmp
    r1 = handle_customer_message("whatsapp:+923001111111", "Join community", **customer_paths)
    assert "What name" in r1.body
    r2 = handle_customer_message("whatsapp:+923001111111", "Sara Ahmed", **customer_paths)
    assert "welcome" in r2.body.lower()
    assert "5 stamps" in r2.body.lower()
    members = load_members(customer_paths["members_path"])
    assert members["+923001111111"].name == "Sara Ahmed"


def test_onboarding_rejects_short_name(community_tmp):
    _, customer_paths, _ = community_tmp
    handle_customer_message("whatsapp:+923007777777", "Hi", **customer_paths)
    r = handle_customer_message("whatsapp:+923007777777", "A", **customer_paths)
    assert "2 characters" in r.body


def test_onboarding_rejects_redeem_code_as_name(community_tmp):
    _, customer_paths, _ = community_tmp
    handle_customer_message("whatsapp:+923008888888", "Hi", **customer_paths)
    r = handle_customer_message("whatsapp:+923008888888", "SR-AB12", **customer_paths)
    assert "name" in r.body.lower()


# ── Customer agent: stamps ──

def test_redeem_gives_stamp_1_of_5(community_tmp):
    _, customer_paths, _ = community_tmp
    _enroll(customer_paths, "+923002222222", "Ali Raza")
    code = issue_redeem_code(customer_paths["redeem_path"], order_id="O1")
    r = handle_customer_message("whatsapp:+923002222222", code.code, **customer_paths)
    assert "Stamp 1/5" in r.body
    assert load_members(customer_paths["members_path"])["+923002222222"].stamps_current == 1


def test_case_insensitive_redeem(community_tmp):
    _, customer_paths, _ = community_tmp
    _enroll(customer_paths, "+923009999991", "Case Test")
    code = issue_redeem_code(customer_paths["redeem_path"])
    r = handle_customer_message("whatsapp:+923009999991", code.code.lower(), **customer_paths)
    assert "Stamp 1/5" in r.body


def test_fifth_stamp_issues_reward_and_resets(community_tmp):
    _, customer_paths, _ = community_tmp
    phone = "+923003333333"
    _enroll(customer_paths, phone, "Hamza")
    last = None
    for i in range(5):
        code = issue_redeem_code(customer_paths["redeem_path"], order_id=f"O{i}")
        last = handle_customer_message(f"whatsapp:{phone}", code.code, **customer_paths)
    assert last is not None
    assert "earned" in last.body.lower() or "5/5" in last.body
    assert load_members(customer_paths["members_path"])[phone].stamps_current == 0
    assert load_members(customer_paths["members_path"])[phone].stamps_lifetime == 5


def test_my_stamps_query(community_tmp):
    _, customer_paths, _ = community_tmp
    _enroll(customer_paths, "+923009999992", "Stamps Query")
    code = issue_redeem_code(customer_paths["redeem_path"])
    handle_customer_message("whatsapp:+923009999992", code.code, **customer_paths)
    r = handle_customer_message("whatsapp:+923009999992", "my stamps", **customer_paths)
    assert "1/5" in r.body


# ── Customer agent: returning guest ──

def test_returning_guest_gets_welcome_back(community_tmp):
    _, customer_paths, _ = community_tmp
    _enroll(customer_paths, "+923009999993", "Return Guest")
    r = handle_customer_message("whatsapp:+923009999993", "Hello", **customer_paths)
    assert "Welcome back" in r.body
    assert "Return Guest" in r.body or "0/5" in r.body


def test_empty_message_gives_help(community_tmp):
    _, customer_paths, _ = community_tmp
    _enroll(customer_paths, "+923009999994", "Help Guest")
    r = handle_customer_message("whatsapp:+923009999994", "", **customer_paths)
    assert "receipt code" in r.body.lower()


def test_unknown_short_message_gives_help_not_llm(community_tmp):
    _, customer_paths, _ = community_tmp
    _enroll(customer_paths, "+923009999995", "Short Msg")
    r = handle_customer_message("whatsapp:+923009999995", "ok", **customer_paths)
    assert "receipt code" in r.body.lower() or "stamps" in r.body.lower()


def test_menu_query_includes_menu_items(community_tmp):
    _, customer_paths, _ = community_tmp
    _enroll(customer_paths, "+923009999996", "Menu Guest")
    r = handle_customer_message("whatsapp:+923009999996", "what's on the menu?", **customer_paths)
    # Without API key, fallback still mentions menu/stamps
    assert "menu" in r.body.lower() or "stamps" in r.body.lower() or "Latte" in r.body


def test_leaderboard_query(community_tmp):
    _, customer_paths, _ = community_tmp
    _enroll(customer_paths, "+923009999997", "Board Guest")
    code = issue_redeem_code(customer_paths["redeem_path"])
    handle_customer_message("whatsapp:+923009999997", code.code, **customer_paths)
    r = handle_customer_message("whatsapp:+923009999997", "leaderboard", **customer_paths)
    assert "Board Guest" in r.body or "stamp" in r.body.lower()


# ── Merchant agent ──

def test_merchant_rejects_non_owner(community_tmp):
    _, _, merchant_paths = community_tmp
    r = handle_merchant_message("whatsapp:+923009999999", "Who is loyal?", **merchant_paths)
    assert "owners only" in r.body.lower()


def test_merchant_stats(community_tmp):
    _, customer_paths, merchant_paths = community_tmp
    _enroll(customer_paths, "+923001234567", "Owner Test")
    r = handle_merchant_message("whatsapp:+923001234567", "How many members?", **merchant_paths)
    assert "Community members" in r.body


def test_merchant_loyal_customers(community_tmp):
    _, customer_paths, merchant_paths = community_tmp
    _enroll(customer_paths, "+923001234567", "Top Guest")
    for _ in range(3):
        code = issue_redeem_code(customer_paths["redeem_path"])
        handle_customer_message("whatsapp:+923001234567", code.code, **customer_paths)
    r = handle_merchant_message("whatsapp:+923001234567", "Who is my loyal customer?", **merchant_paths)
    assert "Top Guest" in r.body or "stamps" in r.body.lower()


def test_merchant_menu_query(community_tmp):
    _, _, merchant_paths = community_tmp
    r = handle_merchant_message("whatsapp:+923001234567", "What's new on the menu?", **merchant_paths)
    assert "MENU" in r.body or "Matcha" in r.body or "Coffee" in r.body


def test_merchant_leaderboard(community_tmp):
    _, customer_paths, merchant_paths = community_tmp
    _enroll(customer_paths, "+923001234567", "LB Guest")
    code = issue_redeem_code(customer_paths["redeem_path"])
    handle_customer_message("whatsapp:+923001234567", code.code, **customer_paths)
    r = handle_merchant_message("whatsapp:+923001234567", "leaderboard", **merchant_paths)
    assert "stamp" in r.body.lower()


def test_merchant_empty_message(community_tmp):
    _, _, merchant_paths = community_tmp
    r = handle_merchant_message("whatsapp:+923001234567", "", **merchant_paths)
    assert "loyal" in r.body.lower() or "stats" in r.body.lower()


# ── Jobs ──

def test_winback_sends_to_inactive_members(community_tmp):
    tmp_path, customer_paths, _ = community_tmp
    members = {
        "+923001100000": CommunityMember(
            phone="+923001100000",
            name="Inactive",
            stamps_current=2,
            joined_at="2020-01-01T00:00:00+00:00",
            last_activity_at=(datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
            opted_in=True,
        ),
    }
    save_members(customer_paths["members_path"], members)
    with patch("app.jobs.winback.send_whatsapp_text") as mock:
        n = run_winback(customer_paths["members_path"], customer_paths["config_path"])
    assert n == 1
    assert "miss you" in mock.call_args[0][1].lower()


def test_winback_skips_recent_members(community_tmp):
    _, customer_paths, _ = community_tmp
    members = {
        "+923001100001": CommunityMember(
            phone="+923001100001",
            name="Active",
            last_activity_at=datetime.now(timezone.utc).isoformat(),
            opted_in=True,
        ),
    }
    save_members(customer_paths["members_path"], members)
    with patch("app.jobs.winback.send_whatsapp_text") as mock:
        n = run_winback(customer_paths["members_path"], customer_paths["config_path"])
    assert n == 0
    mock.assert_not_called()


def test_leaderboard_broadcast(community_tmp):
    _, customer_paths, _ = community_tmp
    members = {
        "+923001200000": CommunityMember(
            phone="+923001200000", name="A", opted_in=True,
        ),
        "+923001200001": CommunityMember(
            phone="+923001200001", name="B", opted_in=True,
        ),
    }
    save_members(customer_paths["members_path"], members)
    append_stamp_event(customer_paths["events_path"], StampEvent(
        phone="+923001200000", code="SR-TEST", stamp_number=1,
        at=datetime.now(timezone.utc).isoformat(),
    ))
    with patch("app.jobs.leaderboard_broadcast.send_whatsapp_text") as mock:
        n = run_leaderboard_broadcast(
            customer_paths["members_path"], customer_paths["events_path"],
        )
    assert n == 2
    assert "leaderboard" in mock.call_args[0][1].lower() or "stamp" in mock.call_args[0][1].lower()


# ── API / webhooks ──

@pytest.fixture
def api_client(community_tmp, monkeypatch):
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    return TestClient(app)


def test_health(api_client):
    r = api_client.get("/health")
    assert r.status_code == 200
    assert r.json()["venue"] == "Sugar Rush"


def test_receipt_api(api_client):
    r = api_client.post("/staff/receipt", json={"order_id": "TEST-1"})
    assert r.status_code == 200
    data = r.json()
    assert data["code"].startswith("SR-")
    assert "SR-" in data["message"]


def test_customer_webhook_e2e(api_client):
    with patch("app.agents.community_customer.send_whatsapp_text") as mock:
        r = api_client.post(
            "/webhooks/twilio/customer",
            data={"From": "whatsapp:+923001111111", "Body": "Hi"},
        )
        assert r.status_code == 200
        mock.assert_called_once()
        assert "What name" in mock.call_args[0][1]


def test_merchant_webhook_e2e(api_client):
    with patch("app.agents.community_merchant.send_whatsapp_text") as mock:
        r = api_client.post(
            "/webhooks/twilio/merchant",
            data={"From": "whatsapp:+923009999999", "Body": "stats"},
        )
        assert r.status_code == 200
        mock.assert_called_once()
        assert "owners only" in mock.call_args[0][1].lower()


def test_process_customer_reply_sends_message(community_tmp):
    _, customer_paths, _ = community_tmp
    with patch("app.agents.community_customer.send_whatsapp_text") as mock:
        process_customer_reply("whatsapp:+923001111111", "Hi", customer_paths, use_twilio=False)
        mock.assert_called_once()


def test_full_guest_journey(community_tmp):
    """End-to-end: QR join → redeem → stamp progress → reward."""
    _, customer_paths, _ = community_tmp
    phone = "whatsapp:+923001300000"

    r1 = handle_customer_message(phone, "Join Sugar Rush", **customer_paths)
    assert "What name" in r1.body

    r2 = handle_customer_message(phone, "Journey Guest", **customer_paths)
    assert "welcome" in r2.body.lower()

    stamps = []
    for i in range(5):
        code = issue_redeem_code(customer_paths["redeem_path"], order_id=f"J{i}")
        r = handle_customer_message(phone, code.code, **customer_paths)
        stamps.append(r.body)

    assert "Stamp 1/5" in stamps[0]
    assert "Stamp 2/5" in stamps[1]
    assert "earned" in stamps[4].lower() or "5/5" in stamps[4]

    member = load_members(customer_paths["members_path"])["+923001300000"]
    assert member.stamps_lifetime == 5
    assert member.stamps_current == 0

    r_back = handle_customer_message(phone, "Hi again", **customer_paths)
    assert "Welcome back" in r_back.body
