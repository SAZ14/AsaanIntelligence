"""Maître d' — reservations and the door.

Takes and confirms bookings over WhatsApp, runs the waitlist, predicts and
cuts no-shows, and recognises a VIP the moment they book.

This package is fully self-contained and does not modify any other agent.

Layout:
    config.py    venue capacity + service windows + VIP list (file-backed defaults)
    models.py    Pydantic models for guests / reservations / waitlist
    store.py     SQLite persistence (reservations, waitlist, guests, conversations)
    noshow.py    heuristic no-show risk scoring
    nlu.py       Claude-driven natural-language understanding (with deterministic fallback)
    agent.py     the decision engine — code makes every booking/door decision
    whatsapp.py  Twilio WhatsApp inbound parsing + outbound client (TwiML / REST)
    api.py       FastAPI app exposing the WhatsApp webhook
"""

from app.maitre_d.agent import MaitreD, MaitreDReply, run_maitre_d_agent

__all__ = ["MaitreD", "MaitreDReply", "run_maitre_d_agent"]
