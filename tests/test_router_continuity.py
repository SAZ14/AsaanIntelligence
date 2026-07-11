"""Router continuity safety net (gateway/internal.py's _topic_signal /
_load_last_agent / _save_last_agent, wired into handle_internal_for_store).

Confirmed live: even at temperature=0, the LLM router occasionally
misroutes a short, keyword-free continuation ("draft a reply I can send
them" mid-reviews-conversation) to a completely unrelated agent. This is a
zero-latency, deterministic correction layered under the LLM call: if the
message itself contains no clear topic keyword AND there's conversation
history, prefer staying with whichever agent handled the previous turn
over trusting a possibly-flaky classification.
"""
from unittest.mock import patch

import pytest

from tests.conftest import seed_chain, seed_store


@pytest.fixture
def store_id():
    chain_id = seed_chain("Continuity Chain")
    return seed_store(chain_id, name="Continuity Cafe", location="F-6, Islamabad")


@pytest.fixture
def fake_redis(monkeypatch):
    fakeredis = pytest.importorskip("fakeredis")
    import app.core.cache as cache
    monkeypatch.setattr(cache, "_client", fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(cache, "_unavailable", False)


PHONE = "whatsapp:+923001234567"


# ── _topic_signal ────────────────────────────────────────────────────────────

def test_topic_signal_detects_reputation_keywords():
    from app.gateway.internal import _topic_signal
    assert _topic_signal("what are people complaining about") == "reputation"
    assert _topic_signal("show me the reviews") == "reputation"


def test_topic_signal_detects_revenue_keywords():
    from app.gateway.internal import _topic_signal
    assert _topic_signal("how is our revenue looking") == "revenue"


def test_topic_signal_detects_integrity_keywords():
    from app.gateway.internal import _topic_signal
    assert _topic_signal("any theft or leakage lately") == "integrity"


def test_topic_signal_detects_scout_keywords():
    from app.gateway.internal import _topic_signal
    assert _topic_signal("what are competitors doing") == "scout"


def test_topic_signal_none_for_vague_continuation():
    from app.gateway.internal import _topic_signal
    assert _topic_signal("draft a reply I can send them") is None
    assert _topic_signal("what about that one") is None
    assert _topic_signal("when was that") is None


# ── _load_last_agent / _save_last_agent ─────────────────────────────────────

def test_last_agent_round_trips(store_id, fake_redis):
    from app.gateway.internal import _load_last_agent, _save_last_agent
    assert _load_last_agent(store_id, PHONE) is None
    _save_last_agent(store_id, PHONE, "reputation")
    assert _load_last_agent(store_id, PHONE) == "reputation"


# ── End-to-end override behavior ────────────────────────────────────────────

def test_override_kicks_in_for_vague_continuation_after_reputation(store_id, fake_redis):
    """The exact confirmed-live failure case: LLM router says "revenue"
    for a bare "draft a reply", but the previous turn was reputation and
    the message names no topic of its own -- must stay on reputation."""
    from app.gateway.internal import handle_internal_for_store, _save_last_agent, _save_staff_turn

    _save_last_agent(store_id, PHONE, "reputation")
    _save_staff_turn(
        store_id, PHONE, [],
        "has anyone complained about delivery being slow",
        "Yes, one review from a customer named Qasim mentions a 2-hour delivery delay.",
    )

    with (
        patch("app.gateway.internal._classify_with_llm", return_value=("revenue", "general")),
        patch("app.gateway.internal._reputation", return_value="Here's a draft reply...") as mock_rep,
        patch("app.gateway.internal._revenue_answer") as mock_rev,
    ):
        reply = handle_internal_for_store(PHONE, "draft a reply I can send them", store_id)

    mock_rep.assert_called_once()
    mock_rev.assert_not_called()
    assert reply == "Here's a draft reply..."


def test_override_does_not_fire_when_message_has_a_real_topic_signal(store_id, fake_redis):
    """A genuine topic switch ("what about our revenue this week" right
    after a reputation conversation) must NOT be overridden -- the
    message clearly names a different domain."""
    from app.gateway.internal import handle_internal_for_store, _save_last_agent, _save_staff_turn

    _save_last_agent(store_id, PHONE, "reputation")
    _save_staff_turn(store_id, PHONE, [], "how are our reviews", "Mostly positive this week.")

    with (
        patch("app.gateway.internal._classify_with_llm", return_value=("revenue", "general")),
        patch("app.gateway.internal._revenue_answer", return_value="Revenue is up 10%.") as mock_rev,
        patch("app.gateway.internal._reputation") as mock_rep,
    ):
        reply = handle_internal_for_store(PHONE, "how is our revenue doing this week", store_id)

    mock_rev.assert_called_once()
    mock_rep.assert_not_called()
    assert reply == "Revenue is up 10%."


def test_override_does_not_fire_on_the_first_message(store_id, fake_redis):
    """No history yet -- nothing to be continuing, so the LLM's answer
    must be trusted as-is even though no last_agent exists either."""
    from app.gateway.internal import handle_internal_for_store

    with (
        patch("app.gateway.internal._classify_with_llm", return_value=("revenue", "general")),
        patch("app.gateway.internal._revenue_answer", return_value="Revenue reply.") as mock_rev,
    ):
        reply = handle_internal_for_store(PHONE, "draft a reply I can send them", store_id)

    mock_rev.assert_called_once()
    assert reply == "Revenue reply."


def test_override_does_not_fire_when_agent_already_matches(store_id, fake_redis):
    """No mismatch to correct -- the LLM already agrees with the last
    agent, so the override path shouldn't even evaluate _topic_signal."""
    from app.gateway.internal import handle_internal_for_store, _save_last_agent, _save_staff_turn

    _save_last_agent(store_id, PHONE, "reputation")
    _save_staff_turn(store_id, PHONE, [], "how are our reviews", "Mostly positive.")

    with (
        patch("app.gateway.internal._classify_with_llm", return_value=("reputation", "chat")),
        patch("app.gateway.internal._reputation", return_value="Still mostly positive.") as mock_rep,
    ):
        reply = handle_internal_for_store(PHONE, "anything new", store_id)

    mock_rep.assert_called_once()
    assert reply == "Still mostly positive."


def test_last_agent_saved_after_llm_routed_turn(store_id, fake_redis):
    from app.gateway.internal import handle_internal_for_store, _load_last_agent

    with (
        patch("app.gateway.internal._classify_with_llm", return_value=("integrity", "summary")),
        patch("app.gateway.internal._integrity", return_value="Summary text."),
    ):
        handle_internal_for_store(PHONE, "how did we do", store_id)

    assert _load_last_agent(store_id, PHONE) == "integrity"


def test_last_agent_saved_after_shorthand_turn(store_id, fake_redis):
    """Shorthand commands skip the LLM router entirely, but must still
    update last_agent so a LATER natural-language follow-up ("why is
    that number so high") has continuity to fall back on."""
    from app.gateway.internal import handle_internal_for_store, _load_last_agent

    with patch("app.gateway.internal._reputation", return_value="ok"):
        handle_internal_for_store(PHONE, "next", store_id)

    assert _load_last_agent(store_id, PHONE) == "reputation"
