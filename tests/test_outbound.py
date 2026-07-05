"""Per-store outbound provider resolution for business-initiated sends."""
import pytest
from unittest.mock import patch

from app.core.outbound import resolve_store_sender, send_from_store, _digits
from tests.conftest import TestSession, seed_chain, seed_store, seed_twilio


@pytest.fixture
def store_id():
    return seed_store(seed_chain("Outbound Chain"), name="Outbound Cafe")


def _register_meta(store_id):
    from app.core.db import StoreMetaNumber
    with TestSession() as db:
        db.add(StoreMetaNumber(store_id=store_id, phone_number_id="pn-123",
                               waba_id="w1", access_token="tok", display_number="+92333"))
        db.commit()


def _register_openwa(store_id):
    from app.core.db import StoreOpenWASession
    with TestSession() as db:
        db.add(StoreOpenWASession(store_id=store_id, session_id="sess-9",
                                  phone_number="+92334"))
        db.commit()


def test_digits_normalization():
    assert _digits("whatsapp:+923001234567") == "923001234567"
    assert _digits("+92 300 1234567") == "923001234567"
    assert _digits("923001234567") == "923001234567"


def test_no_provider_returns_none(store_id):
    assert resolve_store_sender(store_id) is None
    assert send_from_store(store_id, "whatsapp:+92300", "hi") is False


def test_twilio_only(store_id):
    seed_twilio(store_id, "whatsapp:+14155550000")
    provider, send = resolve_store_sender(store_id)
    assert provider == "twilio"
    with patch("app.core.twilio_send.send_whatsapp") as mock:
        send("whatsapp:+923001112223", "hello")
    mock.assert_called_once_with(to="whatsapp:+923001112223", body="hello",
                                 from_="whatsapp:+14155550000")


def test_openwa_preferred_over_twilio(store_id):
    seed_twilio(store_id, "whatsapp:+14155550000")
    _register_openwa(store_id)
    provider, send = resolve_store_sender(store_id)
    assert provider == "openwa"
    with patch("app.core.openwa_send.send_openwa") as mock:
        send("whatsapp:+923001112223", "hello")
    mock.assert_called_once_with("sess-9", "923001112223@c.us", "hello")


def test_meta_preferred_over_everything(store_id):
    seed_twilio(store_id, "whatsapp:+14155550000")
    _register_openwa(store_id)
    _register_meta(store_id)
    provider, send = resolve_store_sender(store_id)
    assert provider == "meta"
    with patch("app.core.meta_send.send_meta") as mock:
        send("whatsapp:+923001112223", "hello")
    mock.assert_called_once_with("pn-123", "923001112223", "hello")


def test_send_from_store_uses_resolved_provider(store_id):
    _register_meta(store_id)
    with patch("app.core.meta_send.send_meta") as mock:
        assert send_from_store(store_id, "+923001112223", "nudge") is True
    mock.assert_called_once_with("pn-123", "923001112223", "nudge")


def test_winback_sends_via_resolved_provider(store_id):
    """The win-back job must follow the store's provider, not hardcode Twilio."""
    from datetime import datetime, timedelta, timezone
    from app.agents.customer.community.store import save_members
    from app.agents.customer.community.models import CommunityMember

    _register_meta(store_id)
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    member = CommunityMember(phone="whatsapp:+923007770001", name="Zara",
                             opted_in=True, joined_at=old, last_activity_at=old)
    save_members(store_id, {member.phone: member})

    from app.agents.customer.jobs.winback import run_winback_for_store
    with patch("app.core.meta_send.send_meta") as mock:
        run_winback_for_store(store_id)
    mock.assert_called_once()
    pnid, to, body = mock.call_args[0]
    assert pnid == "pn-123"
    assert to == "923007770001"
    assert "miss you" in body


def test_broadcast_sends_via_resolved_provider(store_id):
    from app.agents.customer.community.store import save_members
    from app.agents.customer.community.models import CommunityMember

    _register_openwa(store_id)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    member = CommunityMember(phone="whatsapp:+923007770002", name="Omar", opted_in=True,
                             joined_at=now, last_activity_at=now)
    save_members(store_id, {member.phone: member})

    from app.agents.customer.jobs.leaderboard_broadcast import broadcast_for_store
    with patch("app.core.openwa_send.send_openwa") as mock:
        sent = broadcast_for_store(store_id)
    assert sent == 1
    session_id, jid, _body = mock.call_args[0]
    assert session_id == "sess-9"
    assert jid == "923007770002@c.us"
