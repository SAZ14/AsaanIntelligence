"""Tests for WhatsApp onboarding session TTL / pruning."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.whatsapp_sessions import (
    SESSION_TTL_HOURS,
    get_session,
    load_sessions,
    prune_sessions,
    save_sessions,
    set_awaiting_name,
)


def _age_session(path, phone, hours_ago):
    sessions = load_sessions(path)
    key = next(iter(sessions))
    sessions[key].updated_at = (
        datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    ).isoformat()
    save_sessions(path, sessions)


def test_fresh_session_returned(tmp_path):
    path = tmp_path / "sessions.json"
    set_awaiting_name(path, "+923001112222", "sugar-rush")
    session = get_session(path, "+923001112222")
    assert session is not None
    assert session.state == "awaiting_name"


def test_expired_session_dropped_on_get(tmp_path):
    path = tmp_path / "sessions.json"
    set_awaiting_name(path, "+923001112222", "sugar-rush")
    _age_session(path, "+923001112222", SESSION_TTL_HOURS + 1)

    assert get_session(path, "+923001112222") is None
    # get_session also evicts it from disk.
    assert load_sessions(path) == {}


def test_prune_drops_only_expired(tmp_path):
    path = tmp_path / "sessions.json"
    set_awaiting_name(path, "+923001112222", "sugar-rush")  # will be aged out
    set_awaiting_name(path, "+923003334444", "sugar-rush")  # stays fresh
    sessions = load_sessions(path)
    sessions["+923001112222"].updated_at = (
        datetime.now(timezone.utc) - timedelta(hours=SESSION_TTL_HOURS + 5)
    ).isoformat()
    save_sessions(path, sessions)

    removed = prune_sessions(path)
    assert removed == 1
    remaining = load_sessions(path)
    assert "+923003334444" in remaining
    assert "+923001112222" not in remaining
