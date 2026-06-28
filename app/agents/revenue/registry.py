"""DB-driven tenant registry for the revenue agent.

Replaces the JSON-file TenantRegistry from the original branch.
Each store's revenue configuration is read from the revenue_connections table
in Supabase. Agents are cached in-process.
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

    def invalidate(self, store_id: int) -> None:
        self._agents.pop(store_id, None)


# Module-level singleton
_registry: RevenueRegistry | None = None


def get_registry() -> RevenueRegistry:
    global _registry
    if _registry is None:
        _registry = RevenueRegistry()
    return _registry
