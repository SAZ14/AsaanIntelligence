"""Tests for the WhatsApp integration.

Covers: interactive payload structure, the Meta verification handshake, and
button-reply parsing. Everything runs with DRY_RUN — no real Cloud API calls.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from whatsapp.config import WhatsAppConfig
from whatsapp.notifier import (
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
from whatsapp.webhook import (
    dispatch,
    parse_twilio_inbound,
    parse_webhook_events,
    router,
)


def _twilio_cfg(dry_run=True):
    return WhatsAppConfig(
        twilio_account_sid="ACxxxx",
        twilio_auth_token="tok",
        twilio_whatsapp_number="+14155238886",
        dry_run=dry_run,
    )


# ── Fixtures ──

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


# ── Payload structure ──

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
        assert "Bilal" in body            # reconstructed "true story"
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


# ── Twilio send formatting ──

class TestTwilioFormatting:
    def test_wa_address_normalisation(self):
        assert wa_address("+14155238886") == "whatsapp:+14155238886"
        assert wa_address("14155238886") == "whatsapp:+14155238886"
        assert wa_address("whatsapp:+14155238886") == "whatsapp:+14155238886"

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
        assert ACTION_PROMPT in body  # POST / EDIT / IGNORE prompt

    def test_alert_body_respects_1024_limit(self):
        body = twilio_alert_body(_review(), _correlation(), "service_speed", "x" * 5000)
        assert len(body) <= 1024


# ── DRY_RUN: no Twilio calls ──

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


# ── Meta verification handshake ──

class TestWebhookVerification:
    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "s3cret-verify")
        monkeypatch.setenv("DRY_RUN", "true")
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_handshake_echoes_challenge_on_match(self, client):
        resp = client.get("/webhook", params={
            "hub.mode": "subscribe",
            "hub.verify_token": "s3cret-verify",
            "hub.challenge": "1158201444",
        })
        assert resp.status_code == 200
        assert resp.text == "1158201444"

    def test_handshake_rejects_wrong_token(self, client):
        resp = client.get("/webhook", params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "1158201444",
        })
        assert resp.status_code == 403

    def test_handshake_rejects_wrong_mode(self, client):
        resp = client.get("/webhook", params={
            "hub.mode": "unsubscribe",
            "hub.verify_token": "s3cret-verify",
            "hub.challenge": "1158201444",
        })
        assert resp.status_code == 403


# ── Button-reply parsing ──

def _button_reply_body(button_id: str):
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA_ID",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": "PNID"},
                    "messages": [{
                        "from": "923001234567",
                        "id": "wamid.ABC",
                        "timestamp": "1700000000",
                        "type": "interactive",
                        "interactive": {
                            "type": "button_reply",
                            "button_reply": {"id": button_id, "title": "x"},
                        },
                    }],
                },
            }],
        }],
    }


def _text_body(text: str):
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "messages": [{
                        "from": "923001234567",
                        "id": "wamid.TXT",
                        "type": "text",
                        "text": {"body": text},
                    }],
                },
            }],
        }],
    }


class TestButtonReplyParsing:
    def test_parses_button_reply(self):
        msgs = parse_webhook_events(_button_reply_body(BTN_POST_REPLY))
        assert len(msgs) == 1
        m = msgs[0]
        assert m.kind == "button"
        assert m.button_id == BTN_POST_REPLY
        assert m.from_number == "923001234567"
        assert m.message_id == "wamid.ABC"

    def test_post_reply_button_routes_to_post_reply(self):
        msgs = parse_webhook_events(_button_reply_body(BTN_POST_REPLY))
        assert dispatch(msgs[0]) == "post_reply"

    def test_ignore_button_routes_to_ignore(self):
        msgs = parse_webhook_events(_button_reply_body(BTN_IGNORE))
        assert dispatch(msgs[0]) == "ignore"

    def test_edit_button_routes_to_edit(self):
        msgs = parse_webhook_events(_button_reply_body(BTN_EDIT))
        assert dispatch(msgs[0]) == "edit"

    def test_free_text_routes_to_qa(self):
        msgs = parse_webhook_events(_text_body("are we open on Eid?"))
        assert len(msgs) == 1
        assert msgs[0].kind == "text"
        assert msgs[0].text == "are we open on Eid?"
        assert dispatch(msgs[0]) == "qa"

    def test_status_callback_yields_no_messages(self):
        # Delivery receipts carry "statuses", not "messages".
        body = {"entry": [{"changes": [{"value": {"statuses": [{"id": "x"}]}}]}]}
        assert parse_webhook_events(body) == []

    def test_post_endpoint_dispatches(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        resp = client.post("/webhook", json=_button_reply_body(BTN_IGNORE))
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "actions": ["ignore"]}


# ── Twilio inbound parsing ──

class TestTwilioInbound:
    def _form(self, body: str):
        return {"From": "whatsapp:+923001234567", "MessageSid": "SM123", "Body": body}

    def test_post_keyword_routes_to_post_reply(self):
        msg = parse_twilio_inbound(self._form("POST"))
        assert msg.kind == "button"
        assert msg.button_id == BTN_POST_REPLY
        assert msg.from_number == "whatsapp:+923001234567"
        assert dispatch(msg) == "post_reply"

    def test_keyword_is_case_and_asterisk_insensitive(self):
        assert dispatch(parse_twilio_inbound(self._form("  ignore "))) == "ignore"
        assert dispatch(parse_twilio_inbound(self._form("*Edit*"))) == "edit"
        assert dispatch(parse_twilio_inbound(self._form("post reply"))) == "post_reply"

    def test_free_text_routes_to_qa(self):
        msg = parse_twilio_inbound(self._form("are we open on Eid?"))
        assert msg.kind == "text"
        assert msg.text == "are we open on Eid?"
        assert dispatch(msg) == "qa"

    def test_inbound_endpoint_returns_twiml(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setenv("TWILIO_VALIDATE_SIGNATURE", "false")
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        resp = client.post("/twilio/inbound", data=self._form("POST"))
        assert resp.status_code == 200
        assert "application/xml" in resp.headers["content-type"]
        assert "<Response>" in resp.text
        assert "Google" in resp.text  # post_reply ack mentions posting to Google
