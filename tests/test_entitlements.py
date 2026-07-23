"""Per-store agent entitlements -- a store's package determines which of
the 6 agents (integrity, revenue, scout, reputation, maitre_d, customer)
it may use, enforced at every dispatch point: staff WhatsApp commands
(internal.py), guest WhatsApp messages (customer.py), the staff web
dashboard (dashboard.py), and the background crons.
"""
from __future__ import annotations

import pytest

from tests.conftest import seed_chain, seed_store, seed_member, seed_agent_access
from app.core.entitlements import (
    AGENT_NAMES, has_agent_access, get_store_agents, set_store_agents,
    filter_entitled, not_licensed_message,
)

STAFF_PHONE = "whatsapp:+923001112222"
GUEST_PHONE = "whatsapp:+923003334444"


@pytest.fixture
def store_id():
    chain_id = seed_chain("Entitlements Chain")
    sid = seed_store(chain_id, name="Entitlements Cafe", location="F-7, Islamabad")
    seed_member(sid, STAFF_PHONE, role="owner")
    return sid


# ── core module ──────────────────────────────────────────────────────────────

class TestEntitlementsCore:
    def test_new_store_has_full_access_via_seed_store_default(self, store_id):
        # seed_store() grandfathers every test store into the full package
        # (see conftest.seed_store's docstring) -- mirrors the real prod
        # backfill for stores that predate this feature.
        assert get_store_agents(store_id) == AGENT_NAMES

    def test_store_with_no_rows_at_all_has_no_access(self):
        chain_id = seed_chain("Bare Chain")
        from app.core.db import Store, SessionLocal
        with SessionLocal() as db:
            s = Store(chain_id=chain_id, name="Bare Store")
            db.add(s)
            db.commit()
            db.refresh(s)
            bare_store_id = s.id
        # Deliberately NOT using seed_store() -- this is the true "nobody
        # configured a package yet" state, which must be fail-closed.
        assert get_store_agents(bare_store_id) == set()
        for agent in AGENT_NAMES:
            assert has_agent_access(bare_store_id, agent) is False

    def test_set_store_agents_replaces_the_whole_set(self, store_id):
        set_store_agents(store_id, {"integrity", "revenue"})
        assert get_store_agents(store_id) == {"integrity", "revenue"}
        assert has_agent_access(store_id, "scout") is False
        set_store_agents(store_id, {"scout"})
        assert get_store_agents(store_id) == {"scout"}
        assert has_agent_access(store_id, "integrity") is False

    def test_set_store_agents_rejects_unknown_agent(self, store_id):
        with pytest.raises(ValueError):
            set_store_agents(store_id, {"not_a_real_agent"})

    def test_filter_entitled(self, store_id):
        chain_id = seed_chain("Filter Chain")
        other = seed_store(chain_id, name="Other Cafe")
        set_store_agents(other, set())
        assert filter_entitled([store_id, other], "scout") == [store_id]
        assert filter_entitled([], "scout") == []

    def test_not_licensed_message_names_the_agent(self):
        msg = not_licensed_message("scout")
        assert "Scout" in msg
        assert "plan" in msg.lower()


# ── staff WhatsApp dispatch (internal.py) ────────────────────────────────────

class TestStaffDispatchGating:
    def test_shorthand_command_denied_when_agent_not_licensed(self, store_id):
        set_store_agents(store_id, {"integrity"})  # no revenue
        from app.gateway.internal import handle_internal_for_store
        reply = handle_internal_for_store(STAFF_PHONE, "revenue", store_id)
        assert "plan" in reply.lower()
        assert "revenue advisor" in reply.lower()

    def test_licensed_agent_still_works(self, store_id):
        set_store_agents(store_id, {"integrity"})
        from app.gateway.internal import handle_internal_for_store
        reply = handle_internal_for_store(STAFF_PHONE, "summary", store_id)
        assert "plan" not in reply.lower()

    def test_queue_shorthand_denied_when_maitre_d_not_licensed(self, store_id):
        set_store_agents(store_id, {"integrity"})
        from app.gateway.internal import handle_internal_for_store
        reply = handle_internal_for_store(STAFF_PHONE, "admit", store_id)
        assert "plan" in reply.lower()

    def test_add_vip_shorthand_denied_when_maitre_d_not_licensed(self, store_id):
        set_store_agents(store_id, {"integrity"})
        from app.gateway.internal import handle_internal_for_store
        reply = handle_internal_for_store(STAFF_PHONE, "add vip 03001234567 Ahmed", store_id)
        assert "plan" in reply.lower()

    def test_listing_shorthand_denied_when_maitre_d_not_licensed(self, store_id):
        set_store_agents(store_id, {"integrity"})
        from app.gateway.internal import handle_internal_for_store
        reply = handle_internal_for_store(STAFF_PHONE, "queue", store_id)
        assert "plan" in reply.lower()

    def test_booking_toggle_denied_when_maitre_d_not_licensed(self, store_id):
        set_store_agents(store_id, {"integrity"})
        from app.gateway.internal import handle_internal_for_store
        reply = handle_internal_for_store(STAFF_PHONE, "disable booking", store_id)
        assert "plan" in reply.lower()

    def test_help_text_only_lists_licensed_sections(self, store_id):
        set_store_agents(store_id, {"integrity", "scout"})
        from app.gateway.internal import staff_help_text
        text = staff_help_text("Entitlements Cafe", store_id)
        assert "*Integrity*" in text
        assert "*Scout*" in text
        assert "*Revenue*" not in text
        assert "*Reputation*" not in text
        assert "*Queue*" not in text

    def test_help_text_with_no_agents_says_so(self, store_id):
        set_store_agents(store_id, set())
        from app.gateway.internal import staff_help_text
        text = staff_help_text("Entitlements Cafe", store_id)
        assert "no agents are included" in text.lower()

    def test_mode_menu_bracket_list_only_shows_licensed_agents(self, store_id):
        set_store_agents(store_id, {"integrity", "maitre_d"})
        from app.gateway.main import _mode_menu
        text = _mode_menu("Entitlements Cafe", store_id)
        assert "integrity" in text
        assert "reservations" in text
        assert "revenue" not in text
        assert "scout" not in text
        assert "reputation" not in text

    def test_mode_menu_with_no_agents_still_offers_staff_tools_line(self, store_id):
        set_store_agents(store_id, set())
        from app.gateway.main import _mode_menu
        text = _mode_menu("Entitlements Cafe", store_id)
        assert "1: Staff tools" in text


# ── guest WhatsApp dispatch (customer.py) ────────────────────────────────────

class TestCustomerDispatchGating:
    def test_guest_gets_silence_when_customer_agent_not_licensed(self, store_id):
        set_store_agents(store_id, {"maitre_d"})  # no "customer"
        from app.gateway.customer import handle_customer_for_store
        reply = handle_customer_for_store(GUEST_PHONE, "hi, what's on the menu?", store_id)
        assert reply == ""

    def test_guest_gets_reply_when_customer_agent_licensed(self, store_id, monkeypatch):
        set_store_agents(store_id, {"customer"})
        from app.agents.customer.agents import community_customer

        class _FakeReply:
            body = "Hello! Welcome."

        monkeypatch.setattr(
            community_customer, "handle_customer_message",
            lambda *a, **kw: _FakeReply(),
        )
        from app.gateway.customer import handle_customer_for_store
        reply = handle_customer_for_store(GUEST_PHONE, "hi", store_id)
        assert reply == "Hello! Welcome."

    def test_join_queue_trigger_ignored_when_maitre_d_not_licensed(self, store_id, monkeypatch):
        set_store_agents(store_id, {"customer"})  # no maitre_d
        from app.agents.customer.agents import community_customer

        class _FakeReply:
            body = "fell through to community agent"

        monkeypatch.setattr(
            community_customer, "handle_customer_message",
            lambda *a, **kw: _FakeReply(),
        )
        from app.gateway.customer import handle_customer_for_store
        reply = handle_customer_for_store(GUEST_PHONE, "Join the Queue", store_id)
        # maitre_d not licensed -> the trigger phrase is never special-cased,
        # falls through to the (licensed) community agent instead.
        assert reply == "fell through to community agent"

    def test_join_queue_and_no_customer_agent_either_is_fully_silent(self, store_id):
        set_store_agents(store_id, set())
        from app.gateway.customer import handle_customer_for_store
        reply = handle_customer_for_store(GUEST_PHONE, "Join the Queue", store_id)
        assert reply == ""


# ── staff web dashboard (dashboard.py) ───────────────────────────────────────

class TestDashboardGating:
    @pytest.fixture
    def fake_redis(self, monkeypatch):
        fakeredis = pytest.importorskip("fakeredis")
        import app.core.cache as cache
        monkeypatch.setattr(cache, "_client", fakeredis.FakeRedis(decode_responses=True))
        monkeypatch.setattr(cache, "_unavailable", False)

    @pytest.fixture
    def client(self, fake_redis):
        from fastapi.testclient import TestClient
        from app.gateway.main import app
        with TestClient(app, base_url="https://testserver") as c:
            yield c

    def _login(self, client, store_id):
        from app.core import cache
        client.post("/dashboard/login", data={"phone": "+923001112222"})
        entry = cache.get(f"dashboard_otp:{STAFF_PHONE}")
        code = entry["code"]
        r = client.post("/dashboard/verify", data={"phone": STAFF_PHONE, "code": code}, follow_redirects=False)
        return r

    def test_dashboard_blocked_when_maitre_d_not_licensed(self, client, store_id):
        set_store_agents(store_id, {"integrity"})  # no maitre_d
        r = self._login(client, store_id)
        assert r.status_code == 303
        follow = client.get(r.headers["location"])
        assert follow.status_code == 403
        assert "not included in your plan" in follow.text.lower()

    def test_dashboard_works_when_maitre_d_licensed(self, client, store_id):
        set_store_agents(store_id, {"maitre_d"})
        r = self._login(client, store_id)
        assert r.status_code == 303
        follow = client.get(r.headers["location"])
        assert follow.status_code == 200


# ── admin endpoint ────────────────────────────────────────────────────────────

class TestAdminAgentsEndpoint:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from app.gateway.main import app
        with TestClient(app) as c:
            yield c

    def test_set_and_get_agents(self, client, store_id):
        r = client.post(f"/admin/stores/{store_id}/agents", json={"agents": ["scout", "revenue"]})
        assert r.status_code == 200
        assert set(r.json()["agents"]) == {"revenue", "scout"}
        r = client.get(f"/admin/stores/{store_id}/agents")
        assert set(r.json()["agents"]) == {"revenue", "scout"}
        assert get_store_agents(store_id) == {"revenue", "scout"}

    def test_rejects_unknown_agent(self, client, store_id):
        r = client.post(f"/admin/stores/{store_id}/agents", json={"agents": ["not_real"]})
        assert r.status_code == 400

    def test_unknown_store_404s(self, client):
        r = client.post("/admin/stores/999999/agents", json={"agents": ["scout"]})
        assert r.status_code == 404


# ── background crons skip non-entitled stores ────────────────────────────────

class TestCronEntitlementFiltering:
    def test_scout_cron_skips_non_entitled_store(self, store_id, monkeypatch):
        set_store_agents(store_id, set())
        from app.agents.scout import pipeline
        from app.core import apify_guard
        called = []
        monkeypatch.setattr(pipeline, "_is_scout_fresh", lambda sid: called.append(sid) or True)
        monkeypatch.setattr(apify_guard, "breaker_open", lambda: False)
        pipeline.run_scout_all()
        assert store_id not in called

    def test_maitre_d_cron_skips_non_entitled_store(self, store_id, monkeypatch):
        set_store_agents(store_id, set())
        from app.agents.maitre_d import agent as md_agent
        called = []
        monkeypatch.setattr(
            md_agent, "get_maitre_d",
            lambda sid: (_ for _ in ()).throw(AssertionError(f"should not run for store {sid}")),
        )
        md_agent.run_maitre_d_maintenance_all()
        assert called == []


# ── LLM router only ever proposes a licensed agent ───────────────────────────
# ZAI_API_KEY="" in the test environment (see conftest.py), so get_client()
# always raises and _classify_with_llm falls to its keyword fallback --
# exactly the path these tests exercise. The LLM-prompt-restriction
# behavior itself (the `restriction` string built into the system prompt)
# is verified directly against the mocked client below.

class TestLLMRouterEntitlementAwareness:
    def test_keyword_fallback_never_returns_a_disallowed_agent(self):
        from app.gateway.internal import _classify_with_llm
        # "pricing strategy" keyword-classifies as revenue (see
        # app.core.routing._REVENUE_KEYWORDS) -- with only integrity
        # licensed, it must come back as integrity instead.
        agent, _ = _classify_with_llm("what's our pricing strategy", licensed_agents={"integrity"})
        assert agent == "integrity"

    def test_keyword_fallback_picks_an_allowed_non_integrity_agent_when_integrity_isnt_licensed(self):
        from app.gateway.internal import _classify_with_llm
        agent, _ = _classify_with_llm("random unclassifiable message", licensed_agents={"scout"})
        assert agent == "scout"

    def test_full_package_leaves_classification_unconstrained(self):
        """licensed_agents=None (or the full set) must behave exactly as
        before -- no regression for the common, fully-licensed case."""
        from app.gateway.internal import _classify_with_llm, _ROUTABLE_AGENTS
        agent, _ = _classify_with_llm("what's our pricing strategy", licensed_agents=None)
        assert agent == "revenue"
        agent2, _ = _classify_with_llm("what's our pricing strategy", licensed_agents=_ROUTABLE_AGENTS)
        assert agent2 == "revenue"

    def test_system_prompt_carries_the_restriction_when_package_is_narrowed(self):
        from unittest.mock import MagicMock, patch
        from app.gateway.internal import _classify_with_llm

        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content="integrity/summary")),
        ]
        with patch("app.core.llm.get_client", return_value=fake_client), \
             patch("app.core.llm.get_fast_model", return_value="fast-model"), \
             patch("app.core.llm.nothink_kwargs", return_value={}):
            _classify_with_llm("how are we doing", licensed_agents={"integrity", "maitre_d"})
        sent_messages = fake_client.chat.completions.create.call_args.kwargs["messages"]
        system_msg = sent_messages[0]["content"]
        assert "only includes these agents" in system_msg
        assert "integrity" in system_msg and "maitre_d" in system_msg

    def test_system_prompt_has_no_restriction_for_a_full_package(self):
        from unittest.mock import MagicMock, patch
        from app.gateway.internal import _classify_with_llm, _ROUTABLE_AGENTS

        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content="integrity/summary")),
        ]
        with patch("app.core.llm.get_client", return_value=fake_client), \
             patch("app.core.llm.get_fast_model", return_value="fast-model"), \
             patch("app.core.llm.nothink_kwargs", return_value={}):
            _classify_with_llm("how are we doing", licensed_agents=_ROUTABLE_AGENTS)
        sent_messages = fake_client.chat.completions.create.call_args.kwargs["messages"]
        assert "only includes these agents" not in sent_messages[0]["content"]

    def test_model_ignoring_the_restriction_still_falls_back_to_keyword_routing(self):
        """If the LLM names a disallowed agent anyway (rare, but the model
        isn't guaranteed to follow instructions), _classify_with_llm must
        not return it -- it should fall through to the keyword fallback,
        which is itself also constrained."""
        from unittest.mock import MagicMock, patch
        from app.gateway.internal import _classify_with_llm

        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content="revenue/general")),
        ]
        with patch("app.core.llm.get_client", return_value=fake_client), \
             patch("app.core.llm.get_fast_model", return_value="fast-model"), \
             patch("app.core.llm.nothink_kwargs", return_value={}):
            agent, _ = _classify_with_llm("what's our pricing strategy", licensed_agents={"integrity"})
        assert agent == "integrity"

    def test_end_to_end_via_handle_internal_for_store_never_says_not_licensed_when_an_alternative_exists(self, store_id):
        """A revenue-shaped natural-language question, on a store licensed
        for integrity only, must get an actual (integrity) answer, not a
        "not included in your plan" bounce -- there IS a licensed agent
        available, it's just not the one the message would naively match."""
        set_store_agents(store_id, {"integrity"})
        from app.gateway.internal import handle_internal_for_store
        reply = handle_internal_for_store(STAFF_PHONE, "what's our pricing strategy", store_id)
        assert "plan" not in reply.lower()
