"""Tests for the WhatsApp integration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.whatsapp.config import WhatsAppConfig
from app.whatsapp.notifier import (
    ACTION_PROMPT,
    BTN_EDIT,
    BTN_IGNORE,
    BTN_POST_REPLY,
    build_review_alert_payload,
    build_text_payload,
    build_twilio_params,
    send_review_alert,
    send_text,
    twilio_alert_body,
    wa_address,
)


def _twilio_cfg(dry_run=True):
    return WhatsAppConfig(
        twilio_account_sid="ACxxxx",
        twilio_auth_token="tok",
        twilio_whatsapp_number="+14155238886",
        dry_run=dry_run,
    )


def _review():
    return SimpleNamespace(
        review_id="R007", source="Foodpanda", rating=1,
        reviewer_name="Laiba T.",
        text="Saturday night was chaos — drinks took forever.",
    )


def _correlation():
    return SimpleNamespace(
        confidence="high",
        estimated_date="2026-05-09",
        estimated_hour_range="20:00–22:00",
        order_count_in_window=14,
        matched_staff_name="Bilal",
        match_reasons=["text mentions Saturday", "text mentions ~21:00"],
    )


class TestPayloadStructure:
    def test_interactive_alert_shape(self):
        payload = build_review_alert_payload(
            "923001234567", _review(), _correlation(),
            issue="service_speed", draft="Hi Laiba, you're right — that wait wasn't good enough.",
        )
        assert payload["messaging_product"] == "whatsapp"
        assert payload["to"] == "923001234567"
        assert payload["type"] == "interactive"
        assert payload["interactive"]["type"] == "button"

    def test_three_reply_buttons_with_expected_ids(self):
        payload = build_review_alert_payload(
            "923001234567", _review(), _correlation(), "service_speed", "draft text",
        )
        buttons = payload["interactive"]["action"]["buttons"]
        assert len(buttons) == 3
        ids = [b["reply"]["id"] for b in buttons]
        titles = [b["reply"]["title"] for b in buttons]
        assert ids == [BTN_POST_REPLY, BTN_EDIT, BTN_IGNORE]
        assert titles == ["Post reply", "Edit", "Ignore"]
        for b in buttons:
            assert b["type"] == "reply"
            assert len(b["reply"]["title"]) <= 20

    def test_body_contains_rating_source_story_and_draft(self):
        payload = build_review_alert_payload(
            "923001234567", _review(), _correlation(),
            issue="service_speed", draft="UNIQUE_DRAFT_SENTINEL",
        )
        body = payload["interactive"]["body"]["text"]
        assert "1/5" in body
        assert "Foodpanda" in body
        assert "Bilal" in body
        assert "2026-05-09" in body
        assert "UNIQUE_DRAFT_SENTINEL" in body

    def test_body_respects_1024_char_limit(self):
        payload = build_review_alert_payload(
            "923001234567", _review(), _correlation(),
            issue="service_speed", draft="x" * 5000,
        )
        assert len(payload["interactive"]["body"]["text"]) <= 1024

    def test_no_match_correlation_renders_story(self):
        vague = SimpleNamespace(confidence="none")
        payload = build_review_alert_payload(
            "923001234567", _review(), vague, "other", "draft",
        )
        body = payload["interactive"]["body"]["text"]
        assert "Couldn't tie this to a specific visit" in body

    def test_text_payload_shape(self):
        payload = build_text_payload("923001234567", "hello owner")
        assert payload == {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": "923001234567",
            "type": "text",
            "text": {"preview_url": False, "body": "hello owner"},
        }


class TestTwilioFormatting:
    def test_wa_address_normalisation(self):
        assert wa_address("+14155238886") == "whatsapp:+14155238886"
        assert wa_address("14155238886") == "whatsapp:+14155238886"
        assert wa_address("whatsapp:+14155238886") == "whatsapp:+14155238886"
        assert wa_address("+1 415-523-8886") == "whatsapp:+14155238886"

    def test_build_twilio_params_shape(self):
        params = build_twilio_params("923001234567", "hi", _twilio_cfg())
        assert params == {
            "from_": "whatsapp:+14155238886",
            "to": "whatsapp:+923001234567",
            "body": "hi",
        }

    def test_alert_body_has_story_draft_and_prompt(self):
        body = twilio_alert_body(
            _review(), _correlation(), "service_speed", "UNIQUE_DRAFT_SENTINEL",
        )
        assert "1/5" in body
        assert "Foodpanda" in body
        assert "Bilal" in body
        assert "UNIQUE_DRAFT_SENTINEL" in body
        assert ACTION_PROMPT in body

    def test_alert_body_respects_1024_limit(self):
        body = twilio_alert_body(_review(), _correlation(), "service_speed", "x" * 5000)
        assert len(body) <= 1024


class TestDryRun:
    def test_send_review_alert_dry_run_returns_params(self, capsys):
        result = send_review_alert(
            "923001234567", _review(), _correlation(),
            issue="service_speed", draft="draft", config=_twilio_cfg(),
        )
        assert result["dry_run"] is True
        assert result["provider"] == "twilio"
        assert result["params"]["to"] == "whatsapp:+923001234567"
        assert "Twilio" in capsys.readouterr().out

    def test_send_text_dry_run(self):
        result = send_text("923001234567", "hello", config=_twilio_cfg())
        assert result["dry_run"] is True
        assert result["params"]["body"] == "hello"
