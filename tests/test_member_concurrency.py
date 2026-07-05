"""Member-store concurrency: the whole-dict cache/save pattern must never
lose concurrent registrations or clobber concurrent stamp updates.

Reproduces the race found by live stress testing (5 simultaneous
onboardings → 2 registrations silently lost from the cached snapshot).
"""
from datetime import datetime, timezone

import pytest

from tests.conftest import seed_chain, seed_store


def _member(phone: str, name: str, stamps: int = 0):
    from app.agents.customer.community.models import CommunityMember
    now = datetime.now(timezone.utc).isoformat()
    return CommunityMember(phone=phone, name=name, opted_in=True,
                           stamps_current=stamps, stamps_lifetime=stamps,
                           joined_at=now, last_activity_at=now)


@pytest.fixture
def store_id():
    return seed_store(seed_chain("Race Chain"), name="Race Cafe")


@pytest.fixture
def redis_cache(monkeypatch):
    """Enable the real cache layer with fakeredis — the race lived there."""
    fakeredis = pytest.importorskip("fakeredis")
    import app.core.cache as cache
    r = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(cache, "_client", r)
    monkeypatch.setattr(cache, "_unavailable", False)
    return r


def test_interleaved_registrations_both_survive(store_id, redis_cache):
    from app.agents.customer.community.store import load_members, save_member

    # Two requests each load a snapshot before either registers (both warm
    # the same cached snapshot).
    snap_a = load_members(store_id)
    snap_b = load_members(store_id)
    assert snap_a == {} and snap_b == {}

    # Request A registers X and saves; request B registers Y and saves.
    x = _member("whatsapp:+923001000001", "Xavier")
    snap_a[x.phone] = x
    save_member(store_id, x)

    y = _member("whatsapp:+923001000002", "Yusra")
    snap_b[y.phone] = y
    save_member(store_id, y)

    # A fresh load must see BOTH — before the fix, B's stale snapshot
    # overwrote the cache and X vanished for the cache TTL.
    final = load_members(store_id)
    assert x.phone in final, "first concurrent registration was lost"
    assert y.phone in final, "second concurrent registration was lost"
    assert final[x.phone].name == "Xavier"


def test_subset_save_does_not_clobber_other_members_stamps(store_id, redis_cache):
    from app.agents.customer.community.store import load_members, save_member, save_members

    x = _member("whatsapp:+923001000003", "Xavier", stamps=3)
    save_member(store_id, x)

    # Request B loads a snapshot while X has 3 stamps.
    snap_b = load_members(store_id)
    assert snap_b[x.phone].stamps_current == 3

    # Request A stamps X to 4 and saves.
    x.stamps_current = 4
    x.stamps_lifetime = 4
    save_member(store_id, x)

    # Request B saves its OWN new member (subset save) — X must stay at 4.
    y = _member("whatsapp:+923001000004", "Yusra")
    save_members(store_id, {y.phone: y})

    final = load_members(store_id)
    assert final[x.phone].stamps_current == 4, "stale snapshot clobbered a concurrent stamp update"
    assert y.phone in final


def test_sentence_rejected_as_name(store_id):
    """'do you deliver to bahria town' was accepted as a member name during
    the stress test — question-like inputs must re-prompt instead."""
    from unittest.mock import patch
    from app.agents.customer.agents.community_customer import handle_customer_message
    from app.agents.customer.services.messaging import parse_twilio_whatsapp_phone

    key = parse_twilio_whatsapp_phone("whatsapp:+92300")
    with (
        patch("app.agents.customer.agents.community_customer.load_members", return_value={}),
        patch("app.agents.customer.agents.community_customer.load_onboarding_sessions",
              return_value={key: "awaiting_name"}),
        patch("app.agents.customer.agents.community_customer.load_venue_config") as vc,
        patch("app.agents.customer.agents.community_customer.save_member") as mock_save,
    ):
        vc.return_value.venue_name = "Test Cafe"
        for bad in ("do you deliver to bahria town", "what burgers do you have",
                    "kya aap deliver karte ho please", "one two three four five"):
            reply = handle_customer_message("whatsapp:+92300", bad, store_id=store_id)
            assert "first name" in reply.body.lower(), bad
        mock_save.assert_not_called()

        # Real names still pass
        reply = handle_customer_message("whatsapp:+92300", "Danish", store_id=store_id)
        assert "Danish" in reply.body
        mock_save.assert_called_once()
