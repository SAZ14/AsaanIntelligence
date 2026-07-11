"""DB-driven tenant registry for the revenue agent.

Replaces the JSON-file TenantRegistry from the original branch.
Each store's revenue configuration is read from the revenue_connections table.
Agents are cached in-process.
"""
from __future__ import annotations

import logging
from app.agents.revenue.agent import RevenueAgent, RevenueReply
from app.agents.revenue.config import RevenueConfig
from app.agents.revenue.store import Store

logger = logging.getLogger(__name__)



class RevenueRegistry:
    """Routes owner messages to their store's RevenueAgent. DB-backed."""

    def __init__(self, llm_client=None) -> None:
        self._client = llm_client
        self._agents: dict[int, RevenueAgent] = {}

    def agent_for_store(self, store_id: int) -> RevenueAgent | None:
        if store_id in self._agents:
            return self._agents[store_id]

        from app.core.db import SessionLocal, RevenueConnection, Store as StoreModel
        with SessionLocal() as db:
            conn = db.query(RevenueConnection).filter(RevenueConnection.store_id == store_id).first()
            store = db.query(StoreModel).filter(StoreModel.id == store_id).first()
            if not conn or not store:
                return None
            db_path = conn.db_path or ":memory:"
            data_dir = conn.data_dir
            venue_name = store.name
            cfg_overrides = dict(conn.config or {})

        config = RevenueConfig(venue_name=venue_name, **{
            k: v for k, v in cfg_overrides.items()
            if k in RevenueConfig.__dataclass_fields__
        })

        # Use DB-stored uploads (same source as integrity) when no filesystem path is set
        from pathlib import Path
        use_db = not data_dir or not Path(data_dir).exists()
        if use_db:
            from app.agents.revenue.datasource import load_pos_from_db
            orders, menu, staff = load_pos_from_db(store_id)
            agent = RevenueAgent(
                store=Store(db_path),
                config=config,
                client=self._client,
                orders=orders,
                menu=menu,
                staff=staff,
            )
        else:
            agent = RevenueAgent(
                store=Store(db_path),
                config=config,
                client=self._client,
                data_dir=data_dir,
            )
        self._agents[store_id] = agent
        return agent

    def handle(self, store_id: int, from_phone: str, text: str) -> RevenueReply:
        agent = self.agent_for_store(store_id)
        if agent is None:
            logger.warning("revenue.registry: store=%d not_configured from=%s", store_id, from_phone)
            return RevenueReply(
                text="Revenue analysis isn't configured for this restaurant yet. "
                     "Ask your admin to set it up via /admin/stores/{id}/revenue.",
                intent="unknown", action="unconfigured",
            )
        logger.info("revenue.registry: store=%d from=%s cmd=%s", store_id, from_phone, text.split()[0] if text else "")
        return agent.handle_message(from_phone, text)

    def answer_question(self, store_id: int, text: str, history: list[dict] | None = None) -> str:
        agent = self.agent_for_store(store_id)
        if agent is None:
            return "Revenue analysis isn't configured for this restaurant yet."
        return agent.answer_question(text, history=history)

    def invalidate(self, store_id: int) -> None:
        self._agents.pop(store_id, None)


# Module-level singleton
_registry: RevenueRegistry | None = None


def get_registry() -> RevenueRegistry:
    """RevenueRegistry() was always constructed with llm_client=None here --
    no caller ever passed a real one -- so parse_query() never actually
    used the LLM classifier (app.agents.revenue.nlu._parse_with_llm) and
    silently ran on the crude keyword-regex fallback for every single
    revenue message, forever. Any question that didn't match one of those
    regexes fell straight to a generic "I didn't quite catch that" reply.
    Same root cause blocked the new answer_question() free-form Q&A path
    below from working at all."""
    global _registry
    if _registry is None:
        try:
            from app.core.llm import get_client
            client = get_client()
        except Exception:
            client = None
        _registry = RevenueRegistry(llm_client=client)
    return _registry
