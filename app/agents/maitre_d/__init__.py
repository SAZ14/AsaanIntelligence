"""Maitre D — reservations and the door.

Takes and confirms bookings over WhatsApp, runs the waitlist, predicts and
cuts no-shows, and recognises a VIP the moment they book. Ported from the
standalone maitre-d-agent branch into this server's shared, multi-tenant
Postgres schema and gateway routing (customer mode for guest bookings,
staff mode for door/VIP operations) -- see app/gateway/customer.py and
app/gateway/internal.py for the wiring.

Layout:
    config.py    per-store venue capacity + service windows + VIP list (Postgres-backed)
    models.py    Pydantic models for guests / reservations / waitlist
    store.py     Postgres persistence (reservations, waitlist, guests, conversations)
    noshow.py    heuristic no-show risk scoring
    nlu.py       LLM-driven natural-language understanding (ZAI, with deterministic fallback)
    agent.py     the decision engine — code makes every booking/door decision
    payments.py  pluggable deposit provider (stub only -- no real gateway wired yet)
"""

from app.agents.maitre_d.agent import (
    MaitreD, MaitreDReply, get_maitre_d, run_maitre_d_maintenance_all,
)

__all__ = ["MaitreD", "MaitreDReply", "get_maitre_d", "run_maitre_d_maintenance_all"]
