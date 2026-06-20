"""Tests for the WhatsApp delivery layer.

No real network calls: DRY_RUN is used for the print path and a fake Twilio
client is injected for the send path. Webhook routing is tested directly.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.agents.reputation import VisitContext
from app.models.canonical import Review
from app.whatsapp import notifier
from app.whatsapp.notifier import format_review_alert, send_review_alert
from app.whatsapp import webhook
from app.whatsapp.webhook import (
    EDIT,
    EMPTY,
    FREETEXT,
    IGNORE,
    POST,
    handle_inbound,
    parse_command,
)


# ── Fixtures ──

def _review() -> Review:
    return Review(
        review_id="R007",
        source="Foodpanda",
        rating=1,
        posted_at=datetime(2026, 5, 10, 22, 9, 55),
        reviewer_name="Laiba T.",
        text="Saturday night was chaos — sat down at 9ish and our drinks took forever.",
    )


def _correlation() -> VisitContext:
    return VisitContext(
        estimated_date="2026-05-09",
        estimated_hour_range="20:00–22:00",
        order_count_in_window=6,
        staff_on_duty=["S03"],
        matched_staff_name="Bilal",
        confidence="high",
        match_reasons=["text mentions Saturday", "text mentions ~20:00–22:00"],
    )


# ── Formatting ──

class TestFormatting:
    def test_contains_core_fields(self):
        body = format_review_alert(
            _review(), _correlation(), "service_speed",
            "We're so sorry about the wait that night.", sentiment="negative",
        )
        assert "Foodpanda" in body
        assert "1/5" in body
        assert "negative" in body
        assert "service speed" in body          # underscore humanised
        assert "Laiba T." in body
        assert "drinks took forever" in body     # review text
        assert "We're so sorry" in body          # draft reply

    def test_contains_true_story(self):
        body = format_review_alert(
            _review(), _correlation(), "service_speed", "draft", sentiment="negative",
        )
        assert "2026-05-09" in body
        assert "6 orders" in body
        assert "busy period" in body             # >=5 orders
        assert "Bilal" in body
        assert "confidence: high" in body

    def test_action_prompt_present(self):
        body = format_review_alert(_review(), _correlation(), "service_speed", "draft")
        assert "POST" in body
        assert "EDIT" in body
        assert "IGNORE" in body

    def test_no_match_correlation(self):
        body = format_review_alert(
            _review(), VisitContext(confidence="none"), "other", "draft",
        )
        assert "confidence: none" in body
        assert "No reliable visit match" in body


# ── Sending ──

class _FakeMessage:
    sid = "SM_fake_123"


class _FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeMessage()


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


class TestSending:
    def test_dry_run_prints_and_does_not_send(self, monkeypatch, capsys):
        monkeypatch.setenv("DRY_RUN", "1")
        result = send_review_alert(
            "+923001234567", _review(), _correlation(), "service_speed", "draft",
            sentiment="negative",
        )
        out = capsys.readouterr().out
        assert "[DRY_RUN]" in out
        assert result.dry_run is True
        assert result.sent is False
        assert result.to == "whatsapp:+923001234567"

    def test_send_with_injected_client(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "0")
        monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "+14155238886")
        fake = _FakeClient()
        result = send_review_alert(
            "+923001234567", _review(), _correlation(), "service_speed", "draft",
            client=fake,
        )
        assert result.sent is True
        assert result.sid == "SM_fake_123"
        assert len(fake.messages.calls) == 1
        call = fake.messages.calls[0]
        assert call["to"] == "whatsapp:+923001234567"
        assert call["from_"] == "whatsapp:+14155238886"
        assert "service speed" in call["body"]

    def test_missing_creds_raises_when_not_dry_run(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "0")
        monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
        monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("TWILIO_WHATSAPP_NUMBER", raising=False)
        with pytest.raises(RuntimeError):
            send_review_alert(
                "+923001234567", _review(), _correlation(), "service_speed", "draft",
            )

    def test_whatsapp_prefix_not_doubled(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "1")
        result = send_review_alert(
            "whatsapp:+923001234567", _review(), _correlation(), "service_speed", "draft",
        )
        assert result.to == "whatsapp:+923001234567"


# ── Webhook parsing ──

class TestParseCommand:
    def test_post(self):
        assert parse_command("POST").action == POST

    def test_post_case_insensitive_and_whitespace(self):
        assert parse_command("  post  ").action == POST

    def test_ignore(self):
        assert parse_command("ignore").action == IGNORE

    def test_edit_with_text(self):
        cmd = parse_command("EDIT Thanks so much, please come again!")
        assert cmd.action == EDIT
        assert cmd.text == "Thanks so much, please come again!"

    def test_edit_without_text_is_empty(self):
        assert parse_command("EDIT").action == EMPTY

    def test_free_text(self):
        cmd = parse_command("Sorry about that, we'll do better")
        assert cmd.action == FREETEXT
        assert cmd.text == "Sorry about that, we'll do better"

    def test_empty(self):
        assert parse_command("").action == EMPTY
        assert parse_command("   ").action == EMPTY


# ── Webhook routing ──

class TestHandleInbound:
    def test_post_routes_to_post_reply(self, monkeypatch):
        calls = []
        monkeypatch.setattr(webhook, "post_reply",
                            lambda **kw: calls.append(kw))
        cmd = handle_inbound("POST", review_id="R007", platform="Foodpanda")
        assert cmd.action == POST
        assert len(calls) == 1
        assert calls[0]["review_id"] == "R007"

    def test_edit_routes_text_to_post_reply(self, monkeypatch):
        calls = []
        monkeypatch.setattr(webhook, "post_reply",
                            lambda **kw: calls.append(kw))
        cmd = handle_inbound("EDIT new reply text", review_id="R007")
        assert cmd.action == EDIT
        assert calls[0]["text"] == "new reply text"

    def test_ignore_does_not_post(self, monkeypatch):
        calls = []
        monkeypatch.setattr(webhook, "post_reply",
                            lambda **kw: calls.append(kw))
        cmd = handle_inbound("IGNORE", review_id="R007")
        assert cmd.action == IGNORE
        assert calls == []

    def test_free_text_routes_to_post_reply(self, monkeypatch):
        calls = []
        monkeypatch.setattr(webhook, "post_reply",
                            lambda **kw: calls.append(kw))
        cmd = handle_inbound("just say sorry", review_id="R007")
        assert cmd.action == FREETEXT
        assert calls[0]["text"] == "just say sorry"


# ── HTTP layer (optional, needs httpx/TestClient) ──

class TestWebhookEndpoint:
    def test_inbound_endpoint(self, monkeypatch):
        pytest.importorskip("httpx")
        from fastapi.testclient import TestClient

        calls = []
        monkeypatch.setattr(webhook, "post_reply", lambda **kw: calls.append(kw))
        client = TestClient(webhook.app)
        resp = client.post("/whatsapp/inbound", data={"Body": "POST", "From": "whatsapp:+92300"})
        assert resp.status_code == 200
        assert "<Response>" in resp.text
        assert len(calls) == 1
