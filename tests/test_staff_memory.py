"""Staff conversational memory (gateway/internal.py's _load_staff_history /
_save_staff_turn / _condensed_history) -- short-term chat history for
natural-language staff follow-ups ("what about last week?"), reusing the
customer agent's chat-session storage via a "staff:"-prefixed phone key so
the same phone number's customer-mode and staff-mode conversations never
bleed into each other.
"""
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import seed_chain, seed_store


@pytest.fixture
def store_id():
    chain_id = seed_chain("Memory Chain")
    return seed_store(chain_id, name="Memory Cafe", location="F-7, Islamabad")


PHONE = "whatsapp:+923001234567"


# ── Storage round trip + isolation from customer mode ───────────────────────

def test_save_then_load_round_trips(store_id):
    from app.gateway.internal import _load_staff_history, _save_staff_turn

    history = _load_staff_history(store_id, PHONE)
    assert history == []

    _save_staff_turn(store_id, PHONE, history, "how did we do this week", "You made PKR 50,000.")

    loaded = _load_staff_history(store_id, PHONE)
    assert loaded == [
        {"role": "user", "content": "how did we do this week"},
        {"role": "assistant", "content": "You made PKR 50,000."},
    ]


def test_staff_and_customer_history_are_isolated(store_id):
    """The same phone number can be in customer mode or staff mode at
    different times -- a shared bare-phone key would bleed customer
    chit-chat into staff tool answers and vice versa."""
    from app.gateway.internal import _load_staff_history, _save_staff_turn
    from app.agents.customer.community.store import load_chat_session, save_chat_session

    save_chat_session(store_id, PHONE, [{"role": "user", "content": "do you have burgers?"}])
    assert _load_staff_history(store_id, PHONE) == []  # staff history untouched

    _save_staff_turn(store_id, PHONE, [], "leakage", "PKR 3,000 in voids this week.")
    customer_history = load_chat_session(store_id, PHONE)
    assert customer_history == [{"role": "user", "content": "do you have burgers?"}]  # customer untouched


def test_history_capped_at_six_messages(store_id):
    from app.gateway.internal import _load_staff_history, _save_staff_turn

    history = []
    for i in range(5):
        history = _load_staff_history(store_id, PHONE)
        _save_staff_turn(store_id, PHONE, history, f"question {i}", f"answer {i}")

    final = _load_staff_history(store_id, PHONE)
    assert len(final) == 6
    # Oldest turns fall off -- only the most recent 3 exchanges survive.
    assert final[0]["content"] == "question 2"
    assert final[-1]["content"] == "answer 4"


# ── Condensed history for the router (truncates long assistant replies) ────

def test_condensed_history_truncates_long_assistant_replies():
    from app.gateway.internal import _condensed_history

    long_reply = "x" * 500
    history = [
        {"role": "user", "content": "leakage"},
        {"role": "assistant", "content": long_reply},
    ]
    condensed = _condensed_history(history, max_assistant_len=150)
    assert condensed[0]["content"] == "leakage"  # user turns untouched
    assert len(condensed[1]["content"]) == 153  # 150 + "..."
    assert condensed[1]["content"].endswith("...")


def test_condensed_history_leaves_short_replies_alone():
    from app.gateway.internal import _condensed_history
    history = [{"role": "assistant", "content": "short answer"}]
    assert _condensed_history(history) == history


# ── Router sees history ─────────────────────────────────────────────────────

def test_classify_with_llm_includes_history_in_messages():
    from app.gateway.internal import _classify_with_llm

    mock_client = MagicMock()
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "integrity/weekly"
    mock_client.chat.completions.create.return_value = resp

    history = [
        {"role": "user", "content": "how did we do this week"},
        {"role": "assistant", "content": "You made PKR 50,000 this week."},
    ]
    with patch("app.core.llm.get_client", return_value=mock_client):
        agent, command = _classify_with_llm("what about last week", history=history)

    messages = mock_client.chat.completions.create.call_args.kwargs["messages"]
    assert {"role": "user", "content": "how did we do this week"} in messages
    assert messages[-1] == {"role": "user", "content": "what about last week"}
    assert (agent, command) == ("integrity", "weekly")


# ── End-to-end: handle_internal_for_store loads, threads, and saves ────────

def test_handle_internal_for_store_saves_a_turn_and_threads_it_forward(store_id):
    from app.gateway.internal import handle_internal_for_store, _load_staff_history

    with (
        patch("app.gateway.internal._classify_with_llm", return_value=("integrity", "summary")),
        patch("app.gateway.internal._integrity", return_value="Your leakage this week is PKR 3,000.") as mock_integrity,
    ):
        handle_internal_for_store(PHONE, "how did we do this week", store_id)

    # First call: history is empty (nothing saved yet).
    first_history_arg = mock_integrity.call_args.kwargs["history"]
    assert first_history_arg == []

    saved = _load_staff_history(store_id, PHONE)
    assert saved == [
        {"role": "user", "content": "how did we do this week"},
        {"role": "assistant", "content": "Your leakage this week is PKR 3,000."},
    ]

    with (
        patch("app.gateway.internal._classify_with_llm", return_value=("integrity", "weekly")) as mock_classify,
        patch("app.gateway.internal._integrity", return_value="Last week: PKR 2,500.") as mock_integrity2,
    ):
        handle_internal_for_store(PHONE, "what about last week", store_id)

    # Second call: the router AND the agent both see the first turn as context.
    router_history_arg = mock_classify.call_args.kwargs["history"]
    assert router_history_arg == saved
    second_history_arg = mock_integrity2.call_args.kwargs["history"]
    assert second_history_arg == saved


def test_shorthand_commands_also_save_history_for_later_followups(store_id):
    """Even a deterministic shorthand command's reply becomes useful
    context for a later natural-language follow-up ("why is that leakage
    number so high" right after typing "leakage")."""
    from app.gateway.internal import handle_internal_for_store, _load_staff_history

    with (
        patch("app.gateway.internal._classify_with_llm", return_value=("integrity", "leakage")),
        patch("app.gateway.internal._integrity", return_value="Leakage: PKR 5,000.") as mock_integrity,
    ):
        handle_internal_for_store(PHONE, "leakage", store_id)

    mock_integrity.assert_called_once()
    saved = _load_staff_history(store_id, PHONE)
    assert saved == [
        {"role": "user", "content": "leakage"},
        {"role": "assistant", "content": "Leakage: PKR 5,000."},
    ]
