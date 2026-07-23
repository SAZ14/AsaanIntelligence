"""Per-store agent entitlements -- which of the 6 agents a store's package
includes (integrity, revenue, scout, reputation, maitre_d, customer). This
is the single source of truth checked at every point that could actually
run one of those agents: staff WhatsApp commands (gateway/internal.py),
customer WhatsApp messages (gateway/customer.py), the staff web dashboard
(gateway/dashboard.py), the unauthenticated integrity PDF route and the
main.py keyword fast-paths that bypass internal.py's router entirely, and
the background crons that iterate every store. Backed by
app.core.db.StoreAgentAccess -- a store with no rows there has access to
NOTHING (fail-closed by design, see that model's docstring).
"""
from __future__ import annotations

AGENT_NAMES = {"integrity", "revenue", "scout", "reputation", "maitre_d", "customer"}

AGENT_LABELS = {
    "integrity": "Integrity (POS audit)",
    "revenue": "Revenue Advisor",
    "scout": "Scout (competitor intel)",
    "reputation": "Reputation (reviews)",
    "maitre_d": "Queue (Maitre D)",
    "customer": "Loyalty / community chat",
}


def has_agent_access(store_id: int, agent: str) -> bool:
    from app.core.db import SessionLocal, StoreAgentAccess
    with SessionLocal() as db:
        return db.query(StoreAgentAccess).filter(
            StoreAgentAccess.store_id == store_id,
            StoreAgentAccess.agent == agent,
        ).first() is not None


def get_store_agents(store_id: int) -> set[str]:
    from app.core.db import SessionLocal, StoreAgentAccess
    with SessionLocal() as db:
        rows = db.query(StoreAgentAccess).filter(StoreAgentAccess.store_id == store_id).all()
        return {r.agent for r in rows}


def set_store_agents(store_id: int, agents: set[str]) -> None:
    """Replaces the store's entire package in one shot (not incremental
    add/remove) -- callers always pass the full intended set."""
    from app.core.db import SessionLocal, StoreAgentAccess
    invalid = set(agents) - AGENT_NAMES
    if invalid:
        raise ValueError(f"unknown agent(s): {sorted(invalid)}")
    with SessionLocal() as db:
        db.query(StoreAgentAccess).filter(StoreAgentAccess.store_id == store_id).delete()
        for agent in agents:
            db.add(StoreAgentAccess(store_id=store_id, agent=agent))
        db.commit()


def filter_entitled(store_ids: list[int], agent: str) -> list[int]:
    """Narrows a list of store ids down to those entitled to `agent` --
    used by the background crons (run_scout_all, run_reputation_check_all,
    run_maitre_d_maintenance_all, run_winback_all, broadcast_all), all of
    which otherwise iterate every store in the DB unconditionally. One
    query instead of a per-store check inside each cron's loop body."""
    if not store_ids:
        return []
    from app.core.db import SessionLocal, StoreAgentAccess
    with SessionLocal() as db:
        rows = db.query(StoreAgentAccess.store_id).filter(
            StoreAgentAccess.store_id.in_(store_ids),
            StoreAgentAccess.agent == agent,
        ).all()
        return [r[0] for r in rows]


def not_licensed_message(agent: str) -> str:
    label = AGENT_LABELS.get(agent, agent)
    return f"{label} isn't included in your current plan. Contact support to add it to your package."
