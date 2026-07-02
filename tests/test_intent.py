"""Menu intent classifier tests.

The real embedding model is never loaded here — _embedding_model is mocked
so these tests run without sentence-transformers doing real inference,
matching how test_customer_agent.py mocks the store layer.
"""
from unittest.mock import patch, MagicMock

from app.agents.customer.community.intent import is_menu_intent


def _sim_result(value: float) -> list[MagicMock]:
    """Fake return of sentence_transformers.util.cos_sim(...)[0] whose .max() is `value`."""
    row = MagicMock()
    row.max.return_value = value
    return [row]


# ── Regex fallback: embedding model unavailable ─────────────────────────────

def test_falls_back_to_regex_when_model_unavailable():
    with patch("app.agents.customer.community.intent._embedding_model", return_value=None):
        assert is_menu_intent("menu") is True
        assert is_menu_intent("what do you have") is True
        assert is_menu_intent("my stamps") is False
        assert is_menu_intent("leaderboard") is False


def test_fallback_misses_vague_phrasing_same_as_before():
    """Documents the known gap the embedding path is meant to close."""
    with patch("app.agents.customer.community.intent._embedding_model", return_value=None):
        assert is_menu_intent("I'm hungry") is False
        assert is_menu_intent("what's for lunch?") is False


# ── Embedding path ───────────────────────────────────────────────────────────

def test_embedding_path_used_when_model_available():
    fake_model = MagicMock()
    fake_model.encode.return_value = "query-embedding"

    with (
        patch("app.agents.customer.community.intent._embedding_model", return_value=fake_model),
        patch("app.agents.customer.community.intent._menu_examples", return_value=["ex1"]),
        patch("app.agents.customer.community.intent._not_menu_examples", return_value=["neg1"]),
        patch("sentence_transformers.util.cos_sim", side_effect=[_sim_result(0.9), _sim_result(0.1)]) as mock_cos_sim,
    ):
        result = is_menu_intent("I'm hungry")

    assert result is True
    assert mock_cos_sim.call_count == 2


def test_embedding_path_below_threshold_returns_false():
    fake_model = MagicMock()
    fake_model.encode.return_value = "query-embedding"

    with (
        patch("app.agents.customer.community.intent._embedding_model", return_value=fake_model),
        patch("app.agents.customer.community.intent._menu_examples", return_value=["ex1"]),
        patch("app.agents.customer.community.intent._not_menu_examples", return_value=["neg1"]),
        patch("sentence_transformers.util.cos_sim", side_effect=[_sim_result(0.1), _sim_result(0.05)]),
    ):
        result = is_menu_intent("hi there")

    assert result is False


def test_embedding_path_loses_to_not_menu_even_above_threshold():
    """Margin rule: clearing the menu threshold isn't enough if a non-menu
    example matches even more closely (e.g. "what's up" vs "what's good here")."""
    fake_model = MagicMock()
    fake_model.encode.return_value = "query-embedding"

    with (
        patch("app.agents.customer.community.intent._embedding_model", return_value=fake_model),
        patch("app.agents.customer.community.intent._menu_examples", return_value=["ex1"]),
        patch("app.agents.customer.community.intent._not_menu_examples", return_value=["neg1"]),
        patch("sentence_transformers.util.cos_sim", side_effect=[_sim_result(0.6), _sim_result(0.8)]),
    ):
        result = is_menu_intent("what's up")

    assert result is False


def test_threshold_is_configurable_per_call():
    fake_model = MagicMock()
    fake_model.encode.return_value = "query-embedding"

    with (
        patch("app.agents.customer.community.intent._embedding_model", return_value=fake_model),
        patch("app.agents.customer.community.intent._menu_examples", return_value=["ex1"]),
        patch("app.agents.customer.community.intent._not_menu_examples", return_value=["neg1"]),
        patch("sentence_transformers.util.cos_sim", side_effect=[_sim_result(0.6), _sim_result(0.2)]),
    ):
        assert is_menu_intent("something", threshold=0.5) is True

    with (
        patch("app.agents.customer.community.intent._embedding_model", return_value=fake_model),
        patch("app.agents.customer.community.intent._menu_examples", return_value=["ex1"]),
        patch("app.agents.customer.community.intent._not_menu_examples", return_value=["neg1"]),
        patch("sentence_transformers.util.cos_sim", side_effect=[_sim_result(0.6), _sim_result(0.2)]),
    ):
        assert is_menu_intent("something", threshold=0.7) is False
