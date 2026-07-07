"""No em-dashes in any hardcoded, user-facing message string, from any agent
-- AI-generated or deterministic. The customer agent may use a small number
of contextually relevant emojis (max 3/paragraph); every staff-facing agent
(integrity/revenue/scout/reputation) stays emoji-free.

This is a regression lock on the canonical, highest-traffic hardcoded
strings -- it does not (and cannot) cover every LLM-generated response,
since that depends on the model actually following the system prompt.
"""
from tests.conftest import seed_chain, seed_store


def _no_em_dash(text: str) -> bool:
    return "—" not in text


class TestStaffFacingHardcodedStrings:
    def test_staff_help_text_has_no_em_dash(self):
        from app.gateway.internal import staff_help_text
        assert _no_em_dash(staff_help_text("Test Cafe"))

    def test_integrity_help_text_has_no_em_dash(self):
        from app.agents.integrity.service import HELP_TEXT
        assert _no_em_dash(HELP_TEXT)

    def test_mode_menu_has_no_em_dash(self):
        from app.gateway.main import _mode_menu
        assert _no_em_dash(_mode_menu("Test Cafe"))

    def test_internal_welcome_delegates_to_staff_help_text(self):
        """main.py used to keep its own separate, drifting copy of this
        text -- confirm it's just a thin wrapper now, not a second source
        of truth that could silently reintroduce an em-dash."""
        from app.gateway.main import _internal_welcome
        from app.gateway.internal import staff_help_text
        assert _internal_welcome("Test Cafe") == staff_help_text("Test Cafe")

    def test_internal_ack_messages_have_no_em_dash(self):
        from app.gateway.main import _internal_ack
        for body in ("check reviews", "leakage report", "revenue this week", "random unmatched text"):
            assert _no_em_dash(_internal_ack(body)), f"em-dash in ack for {body!r}"

    def test_store_menu_has_no_em_dash(self):
        from app.core.routing import store_menu
        from app.core.db import Store
        stores = [Store(id=1, name="Alpha", location="DHA"), Store(id=2, name="Beta", location=None)]
        assert _no_em_dash(store_menu(stores))


class TestStaffAgentsStayEmojiFree:
    """Confirmed via user decision: only the customer agent gets the
    emoji allowance. Staff agents' hardcoded strings must not gain
    emojis (LLM prompts are covered separately by explicit "No emojis"
    instructions checked in each agent's own test file)."""

    def _has_emoji(self, text: str) -> bool:
        return any(0x1F300 <= ord(ch) <= 0x1FAFF or 0x2600 <= ord(ch) <= 0x27BF for ch in text)

    def test_staff_help_text_has_no_emoji(self):
        from app.gateway.internal import staff_help_text
        assert not self._has_emoji(staff_help_text("Test Cafe"))

    def test_internal_ack_messages_have_no_emoji(self):
        from app.gateway.main import _internal_ack
        for body in ("check reviews", "leakage report", "revenue this week", "random text"):
            assert not self._has_emoji(_internal_ack(body))

    def test_scout_report_system_prompt_forbids_emojis(self):
        from app.agents.scout.analysis import _report_system
        prompt = _report_system("Test Cafe", "burger restaurant")
        assert "no emojis" in prompt.lower()

    def test_revenue_agent_strings_have_no_emoji(self):
        """revenue/agent.py previously had a hardcoded 💰 and 📈 in its
        deterministic price-advice text."""
        from app.agents.revenue.agent import RevenueAgent
        import inspect
        source = inspect.getsource(RevenueAgent)
        assert not self._has_emoji(source)


class TestCustomerAgentEmojiPolicy:
    def test_customer_prompt_allows_limited_contextual_emojis(self):
        from app.agents.customer.agents.community_customer import _llm_generate
        import inspect
        source = inspect.getsource(_llm_generate)
        assert "max 3 per paragraph" in source
        assert "no em-dashes" in source.lower()
