"""Maitre D — the live walk-in queue and the door.

Takes guests into today's queue over WhatsApp ("book" -> a booking number),
lets staff admit/remove/insert people in the live line, and recognises a
VIP the moment they message. Ported from the standalone maitre-d-agent
branch into this server's shared, multi-tenant Postgres schema and gateway
routing (customer mode for guests joining the queue, staff mode for
admit/VIP operations) -- see app/gateway/customer.py and
app/gateway/internal.py for the wiring.

Layout:
    config.py    per-store venue identity + branch matching + VIP list (Postgres-backed)
    models.py    Pydantic models for guests / queue entries
    store.py     Postgres persistence (the live queue, guests, conversations)
    nlu.py       LLM-driven natural-language understanding (ZAI, with deterministic fallback)
    agent.py     the decision engine — code makes every queue join/leave decision
    staff.py     staff-facing queue admin (admit/remove/insert, VIP list, Q&A)
"""

from app.agents.maitre_d.agent import (
    MaitreD, MaitreDReply, get_maitre_d, run_maitre_d_maintenance_all,
)

__all__ = ["MaitreD", "MaitreDReply", "get_maitre_d", "run_maitre_d_maintenance_all"]
