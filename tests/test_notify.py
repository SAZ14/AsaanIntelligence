"""Tests for the WhatsApp notification layer. No live Twilio calls."""

from __future__ import annotations

import pytest
from twilio.base.exceptions import TwilioRestException

from app.notify import (
    ConfigError,
    ErrorCategory,
    SendResult,
    WhatsAppNotifier,
    WhatsAppSendError,
    WhatsAppSettings,
    classify_code,
    owner_summary,
)
from app.notify.config import SANDBOX_SENDER
from app.report.render import HeadlineNumbers

BASE_ENV = {
    "TWILIO_ACCOUNT_SID": "AC" + "0" * 32,
    "TWILIO_AUTH_TOKEN": "x" * 32,
    "TWILIO_WHATSAPP_NUMBER": "whatsapp:+14155238886",
    "OWNER_NUMBER": "+16292595668",
    "DRY_RUN": "0",
}


# ── config ──

def test_from_env_normalizes_and_detects_sandbox():
    s = WhatsAppSettings.from_env(BASE_ENV)
    assert s.recipient == "whatsapp:+16292595668"  # prefix added
    assert s.sender == SANDBOX_SENDER
    assert s.is_sandbox is True
    assert s.dry_run is False


def test_from_env_accepts_from_to_aliases():
    env = {
        "TWILIO_ACCOUNT_SID": "AC" + "1" * 32,
        "TWILIO_AUTH_TOKEN": "y" * 32,
        "TWILIO_WHATSAPP_FROM": "whatsapp:+1999",
        "TWILIO_WHATSAPP_TO": "whatsapp:+1888",
    }
    s = WhatsAppSettings.from_env(env)
    assert s.sender == "whatsapp:+1999"
    assert s.recipient == "whatsapp:+1888"
    assert s.is_sandbox is False


def test_dry_run_truthy_parsing():
    assert WhatsAppSettings.from_env({**BASE_ENV, "DRY_RUN": "1"}).dry_run is True
    assert WhatsAppSettings.from_env({**BASE_ENV, "DRY_RUN": "true"}).dry_run is True
    assert WhatsAppSettings.from_env({**BASE_ENV, "DRY_RUN": "0"}).dry_run is False


def test_missing_fields_raise():
    with pytest.raises(ConfigError):
        WhatsAppSettings.from_env({"TWILIO_ACCOUNT_SID": "AC1"})


def test_bad_sid_prefix_raises():
    with pytest.raises(ConfigError):
        WhatsAppSettings.from_env({**BASE_ENV, "TWILIO_ACCOUNT_SID": "ZZ123"})


# ── error classification ──

def test_classify_code_mapping():
    assert classify_code(63015) is ErrorCategory.SANDBOX_OPTIN
    assert classify_code(63016) is ErrorCategory.SESSION_WINDOW
    assert classify_code(20003) is ErrorCategory.AUTH
    assert classify_code(63013) is ErrorCategory.RECIPIENT
    assert classify_code(99999) is ErrorCategory.UNKNOWN
    assert classify_code(None) is ErrorCategory.UNKNOWN


# ── fake Twilio client ──

class _FakeMsg:
    def __init__(self, sid, status, error_code=None, error_message=None):
        self.sid = sid
        self.status = status
        self.error_code = error_code
        self.error_message = error_message


class _FakeMessages:
    def __init__(self, on_create=None, fetch_msg=None):
        self._on_create = on_create
        self._fetch_msg = fetch_msg
        self.created = []

    def create(self, **params):
        self.created.append(params)
        if callable(self._on_create):
            return self._on_create(params)
        return self._on_create

    def __call__(self, sid):
        return self  # messages(sid)

    def fetch(self):
        return self._fetch_msg


class _FakeClient:
    def __init__(self, messages):
        self.messages = messages


def _settings(**over):
    return WhatsAppSettings.from_env({**BASE_ENV, **over})


# ── sending ──

def test_dry_run_does_not_call_client():
    msgs = _FakeMessages(on_create=lambda p: _FakeMsg("SM1", "queued"))
    n = WhatsAppNotifier(_settings(DRY_RUN="1"), client=_FakeClient(msgs))
    res = n.send_text("hi")
    assert res.dry_run is True
    assert msgs.created == []  # never contacted Twilio


def test_send_text_success():
    msgs = _FakeMessages(on_create=lambda p: _FakeMsg("SM2", "queued"))
    n = WhatsAppNotifier(_settings(), client=_FakeClient(msgs))
    res = n.send_text("hi")
    assert isinstance(res, SendResult)
    assert res.sid == "SM2"
    assert msgs.created[0]["from_"] == "whatsapp:+14155238886"
    assert msgs.created[0]["to"] == "whatsapp:+16292595668"


def test_send_text_auth_error_classified():
    def boom(_):
        raise TwilioRestException(status=401, uri="/m", msg="Authenticate", code=20003)

    n = WhatsAppNotifier(_settings(), client=_FakeClient(_FakeMessages(on_create=boom)))
    with pytest.raises(WhatsAppSendError) as ei:
        n.send_text("hi")
    assert ei.value.category is ErrorCategory.AUTH
    assert ei.value.code == 20003


def test_network_error_classified():
    def boom(_):
        raise ConnectionError("dns fail")

    n = WhatsAppNotifier(_settings(), client=_FakeClient(_FakeMessages(on_create=boom)))
    with pytest.raises(WhatsAppSendError) as ei:
        n.send_text("hi")
    assert ei.value.category is ErrorCategory.NETWORK


def test_send_and_confirm_polls_to_delivered():
    msgs = _FakeMessages(
        on_create=lambda p: _FakeMsg("SM3", "queued"),
        fetch_msg=_FakeMsg("SM3", "delivered"),
    )
    n = WhatsAppNotifier(_settings(), client=_FakeClient(msgs))
    res = n.send_and_confirm("hi", attempts=2, interval_s=0)
    assert res.status == "delivered"
    assert res.ok is True


def test_send_and_confirm_raises_on_sandbox_failure():
    msgs = _FakeMessages(
        on_create=lambda p: _FakeMsg("SM4", "queued"),
        fetch_msg=_FakeMsg("SM4", "failed", error_code=63015),
    )
    n = WhatsAppNotifier(_settings(), client=_FakeClient(msgs))
    with pytest.raises(WhatsAppSendError) as ei:
        n.send_and_confirm("hi", attempts=2, interval_s=0)
    assert ei.value.category is ErrorCategory.SANDBOX_OPTIN
    assert ei.value.code == 63015
    assert ei.value.message_sid == "SM4"


# ── message rendering ──

def test_owner_summary_contains_headline_numbers():
    h = HeadlineNumbers(
        monthly_leakage=12345,
        venue_wide_leakage_monthly=20000,
        monthly_winback_tier_a=8000,
        monthly_winback_total=11000,
        winback_pct_of_revenue=0.04,
        period_days=35,
    )
    text = owner_summary(h, venue_name="Café X")
    assert "Café X" in text
    assert "PKR 12,345" in text
    assert "PKR 8,000" in text
    assert "4.0% of revenue" in text


def test_owner_summary_sanity_warning():
    h = HeadlineNumbers(winback_sanity_warning=True)
    assert "review assumptions" in owner_summary(h)
