"""A staff member's first "hi" (or any bare greeting) must show the full
staff command list across all four agents, never one agent's own incomplete
view of itself.

Root cause (confirmed via a real production report): "hi" isn't a
recognized shorthand, so it fell through to the LLM intent classifier in
app/gateway/internal.py, which forced it into agent=integrity (its
documented fallback). IntegrityService.handle_message() then had its OWN
"hi"/"hello" special case, returning "Integrity Agent\n\nsummary..." -- a
help text that (a) only knew about integrity's own commands, missing
revenue/scout/reputation entirely, and (b) was itself incomplete (missing
"refresh", which does exist as a real command). Separately, main.py's own
"welcome after selecting mode 1" text was a second, independently drifting
copy of the same idea.

Fixed by making app/gateway/internal.staff_help_text() the one canonical
source, intercepting greetings before the LLM classifier ever runs, and
removing the greeting special-case from IntegrityService (it now only
answers an explicit "help").
"""
from unittest.mock import patch

import pytest

from tests.conftest import seed_chain, seed_store


@pytest.fixture
def store_id():
    chain_id = seed_chain("Greeting Chain")
    return seed_store(chain_id, name="Greeting Cafe", location="F-7, Islamabad")


GREETINGS = ["hi", "Hi", "HELLO", "hey", "salam", "assalamualaikum", "aoa", "start", "commands"]


@pytest.mark.parametrize("greeting", GREETINGS)
def test_greeting_returns_full_staff_help_never_reaches_llm_router(store_id, greeting):
    from app.gateway.internal import handle_internal_for_store
    with patch("app.gateway.internal._classify_with_llm") as mock_classify:
        reply = handle_internal_for_store("+923001234567", greeting, store_id)
    mock_classify.assert_not_called()
    assert "Integrity" in reply and "Revenue" in reply and "Scout" in reply and "Reputation" in reply


def test_greeting_response_lists_every_documented_command(store_id):
    from app.gateway.internal import handle_internal_for_store
    reply = handle_internal_for_store("+923001234567", "hi", store_id)
    for cmd in ("summary", "leakage", "profit", "staff", "daily", "weekly", "refresh",
                "revenue", "sales", "pricing", "strategy", "scout",
                "check", "post", "ignore", "edit"):
        assert cmd in reply.lower(), f"{cmd!r} missing from staff help text"


def test_greeting_response_mentions_natural_language_support(store_id):
    from app.gateway.internal import handle_internal_for_store
    reply = handle_internal_for_store("+923001234567", "hi", store_id)
    assert "plain language" in reply.lower() or "normal language" in reply.lower() or "natural language" in reply.lower()


def test_greeting_response_includes_store_name(store_id):
    from app.gateway.internal import handle_internal_for_store
    reply = handle_internal_for_store("+923001234567", "hi", store_id)
    assert "Greeting Cafe" in reply


def test_help_keyword_returns_same_canonical_text_as_greeting(store_id):
    from app.gateway.internal import handle_internal_for_store
    help_reply = handle_internal_for_store("+923001234567", "help", store_id)
    hi_reply = handle_internal_for_store("+923001234567", "hi", store_id)
    assert help_reply == hi_reply


def test_mode_select_welcome_uses_the_same_canonical_text():
    """main.py's "after selecting 1" welcome used to be a second, separately
    drifting copy of this text (missing "refresh"). Both must now come from
    the exact same function."""
    from app.gateway.main import _internal_welcome
    from app.gateway.internal import staff_help_text
    assert _internal_welcome("Some Cafe") == staff_help_text("Some Cafe")


def test_real_natural_language_query_still_reaches_the_llm_router(store_id):
    """Make sure the greeting intercept is narrow -- a genuine question
    must still go through normal LLM classification, not get swallowed by
    the greeting check. Integrity's natural-language path now passes the
    original question straight through to IntegrityService (whose own
    free-form answer_question() produces an already-tailored answer), not
    through _adapt_response on top of a fixed-template result."""
    from app.gateway.internal import handle_internal_for_store
    with patch("app.gateway.internal._classify_with_llm", return_value=("integrity", "summary")) as mock_classify, \
         patch("app.gateway.internal._integrity", return_value="mocked summary") as mock_integrity:
        reply = handle_internal_for_store("+923001234567", "how did we do this week", store_id)
    mock_classify.assert_called_once()
    mock_integrity.assert_called_once_with(store_id, "+923001234567", "how did we do this week")
    assert reply == "mocked summary"
