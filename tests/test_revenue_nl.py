"""Revenue advisor natural-language handling.

Two real bugs found together: (1) get_registry() always constructed
RevenueRegistry with llm_client=None, so parse_query() never actually used
the LLM classifier -- every message, forever, ran on a crude keyword-regex
fallback that fails silently on anything not matching one of ~10 patterns.
(2) even with a real client, natural-language questions were routed through
handle_message()'s classify-into-fixed-intent-then-template path and only
patched up afterward via a second LLM "adapt" call working from the
generic template's text, not the real underlying numbers -- unlike
scout/reputation/customer, which all give the LLM the real data and the
actual question in one call.
"""
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.agents.revenue.agent import RevenueAgent
from app.models.canonical import MenuItem, Order, LineItem, Payment


def _order(order_id: str) -> Order:
    return Order(
        order_id=order_id, datetime=datetime(2026, 7, 1, 12, 0), staff_id="S1",
        staff_name="Ali", channel="dine_in", order_status="completed",
        line_items=[LineItem(item_sku="BRG1", item_name="Classic Burger", category="burger",
                             qty=2, unit_price=650, line_amount=1300)],
        payments=[Payment(method="cash", amount=1300, tax_rate=0.0)],
    )


def _menu() -> dict[str, MenuItem]:
    return {"BRG1": MenuItem(sku="BRG1", name="Classic Burger", category="burger", price=650, cost=300)}


def _agent(client=None) -> RevenueAgent:
    return RevenueAgent(orders=[_order("O1")], menu=_menu(), staff={}, client=client)


# ── get_registry() must pass a real LLM client ───────────────────────────────

class TestRegistryPassesRealClient:
    def test_get_registry_uses_a_real_client_not_none(self, monkeypatch):
        import app.agents.revenue.registry as registry_mod
        monkeypatch.setattr(registry_mod, "_registry", None)
        sentinel_client = MagicMock()
        with patch("app.core.llm.get_client", return_value=sentinel_client):
            reg = registry_mod.get_registry()
        assert reg._client is sentinel_client

    def test_get_registry_degrades_gracefully_if_no_api_key(self, monkeypatch):
        import app.agents.revenue.registry as registry_mod
        monkeypatch.setattr(registry_mod, "_registry", None)
        with patch("app.core.llm.get_client", side_effect=RuntimeError("no key")):
            reg = registry_mod.get_registry()
        assert reg._client is None


# ── RevenueAgent.answer_question ──────────────────────────────────────────────

class TestRevenueAnswerQuestion:
    def test_no_client_returns_a_graceful_fallback(self):
        agent = _agent(client=None)
        result = agent.answer_question("how did we do this week")
        assert "unavailable" in result.lower()

    def test_calls_llm_once_with_real_data_and_the_actual_question(self):
        mock_client = MagicMock()
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = "You sold 2 Classic Burgers for PKR 1,300 this week."
        mock_client.chat.completions.create.return_value = resp

        agent = _agent(client=mock_client)
        result = agent.answer_question("how many burgers did we sell this week")

        mock_client.chat.completions.create.assert_called_once()
        messages = mock_client.chat.completions.create.call_args.kwargs["messages"]
        full_prompt = "\n".join(m["content"] for m in messages)
        assert "how many burgers did we sell this week" in full_prompt
        assert "Classic Burger" in full_prompt  # real computed data made it into context
        assert "no em-dashes" in full_prompt.lower()
        assert "no emojis" in full_prompt.lower()
        assert result == "You sold 2 Classic Burgers for PKR 1,300 this week."

    def test_llm_failure_returns_an_error_string_not_a_crash(self):
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("timeout")
        agent = _agent(client=mock_client)
        result = agent.answer_question("how did we do this week")
        assert "failed" in result.lower()


# ── internal.py routing: natural language vs exact shorthand ────────────────

class TestInternalRevenueRouting:
    def test_natural_language_routes_to_answer_question_not_template(self):
        from app.gateway.internal import handle_internal_for_store
        with patch("app.gateway.internal._classify_with_llm", return_value=("revenue", "general")), \
             patch("app.gateway.internal._revenue_answer", return_value="direct answer") as mock_answer, \
             patch("app.gateway.internal._revenue") as mock_template:
            result = handle_internal_for_store("+923001234567", "how are we doing with fries", 1)
        mock_answer.assert_called_once()
        mock_template.assert_not_called()
        assert result == "direct answer"

    def test_exact_shorthand_still_uses_the_template_path(self):
        from app.gateway.internal import handle_internal_for_store
        with patch("app.gateway.internal._revenue", return_value="template reply") as mock_template, \
             patch("app.gateway.internal._revenue_answer") as mock_answer:
            result = handle_internal_for_store("+923001234567", "pricing", 1)
        mock_template.assert_called_once()
        mock_answer.assert_not_called()
        assert result == "template reply"
