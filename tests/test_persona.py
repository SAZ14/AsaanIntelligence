"""Unified staff-assistant persona (app.core.persona) -- shared tone/
formatting/grounding baseline reused by integrity, revenue, reputation,
scout, and gateway/internal.py's _adapt_response rewrite pass, so the
four agents read as one consistent assistant instead of four separately
tuned bots. See app/core/persona.py's module docstring for the audit
finding that motivated this (integrity/revenue had no system-role
message at all; each agent duplicated its own slightly different wording
for the same WhatsApp formatting rules).
"""
from unittest.mock import MagicMock, patch


def test_staff_persona_includes_role_line_grounding_and_formatting():
    from app.core.persona import staff_persona
    result = staff_persona("You are a test analyst for Test Cafe.")
    assert "You are a test analyst for Test Cafe." in result
    assert "ONLY the data provided" in result
    assert "never invent" in result.lower()
    assert "*single asterisks*" in result
    assert "no emojis" in result.lower()
    assert "no em-dashes" in result.lower()


def test_grounding_rule_says_say_so_when_unknown():
    from app.core.persona import GROUNDING_RULE
    assert "say so plainly" in GROUNDING_RULE.lower()


# ── Each agent actually uses the shared base, not its own separate copy ────

def test_integrity_system_prompt_uses_shared_persona():
    from app.agents.integrity.agents import integrity_agent

    fake_report = MagicMock()
    fake_report.venue_name = "Test Cafe"
    mock_client = MagicMock()
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "answer"
    mock_client.chat.completions.create.return_value = resp

    with patch.object(integrity_agent, "_context", return_value="fake context"):
        integrity_agent.answer_question(fake_report, "what happened", client=mock_client)

    messages = mock_client.chat.completions.create.call_args.kwargs["messages"]
    system_msg = next(m["content"] for m in messages if m["role"] == "system")
    assert "*single asterisks*" in system_msg
    assert "Test Cafe" in system_msg


def test_scout_report_system_uses_shared_persona():
    from app.agents.scout.analysis import _report_system
    system = _report_system("Test Cafe", "burger joint")
    assert "*single asterisks*" in system
    assert "no emojis" in system.lower()
    assert "ONLY the data provided" in system


def test_reputation_chat_system_uses_shared_persona(monkeypatch):
    """_chat_about_reviews builds its system prompt inline -- confirm the
    shared WhatsApp formatting text appears exactly once (not duplicated
    by reputation's own now-removed copy of the same rules)."""
    import app.agents.reputation as reputation

    monkeypatch.setattr(reputation, "_detect_sentiment_lean", lambda text: None)
    with (
        patch("app.review_sources.db.get_pending_finding", return_value=None),
        patch("app.review_sources.db.search_reviews_semantic", return_value=[]),
        patch("app.review_sources.db.get_recent_reviews", return_value=[]),
        patch("app.core.llm.get_client") as mock_get_client,
        patch("app.core.llm.get_model", return_value="glm-4.7"),
    ):
        mock_client = MagicMock()
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "answer"
        mock_client.chat.completions.create.return_value = resp
        mock_get_client.return_value = mock_client

        reputation._chat_about_reviews(1, "Test Cafe", "how are we doing")

        messages = mock_client.chat.completions.create.call_args.kwargs["messages"]
        system_msg = next(m["content"] for m in messages if m["role"] == "system")
        assert system_msg.count("*single asterisks*") == 1
        assert "Test Cafe" in system_msg
