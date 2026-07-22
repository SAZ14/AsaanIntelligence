"""Pydantic models for the Maitre D domain: guests and the live queue.

Kept separate from app.core.db's SQLAlchemy models (mirroring how scout/
reputation use plain dataclasses/Pydantic models internally and only touch
the ORM at the store.py boundary) -- the engine (agent.py) reasons about
these, not raw DB rows.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


QUEUE_STATUSES = {
    "waiting",      # in the live line
    "admitted",     # staff let them in, seated
    "removed",      # taken out by staff
    "cancelled",    # the guest themselves left the queue
}


class Guest(BaseModel):
    store_id: int = 0
    phone: str                      # normalised E.164, e.g. "+923001234567"
    name: str = ""
    vip_tier: str = ""              # "" if not a VIP
    vip_notes: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class QueueEntry(BaseModel):
    id: int = 0
    store_id: int = 0
    location_id: int = 0
    branch_name: str = ""           # denormalised for display -- avoids a join every time a reply is composed
    queue_number: int = 0           # permanent for the day, told to the guest, never reused
    phone: str
    name: str = ""
    party_size: int = 1
    special_requests: str = ""
    status: str = "waiting"
    position: int = 0               # 1-based, live ordering among this location's "waiting" rows
    is_vip: bool = False
    vip_tier: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    admitted_at: datetime | None = None
