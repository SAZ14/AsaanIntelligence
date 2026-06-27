"""Pydantic models for the Maître d' domain: guests, reservations, waitlist.

Kept separate from ``app.models.canonical`` (the POS audit models) so this
agent stays fully self-contained.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# Reservation lifecycle
RESERVATION_STATUSES = {
    "pending",      # awaiting guest confirmation (e.g. deposit for high no-show risk)
    "confirmed",    # table held
    "seated",       # guest has arrived and been seated
    "completed",    # visit finished
    "cancelled",    # cancelled by guest or venue
    "no_show",      # never arrived
}

WAITLIST_STATUSES = {
    "waiting",      # in the queue
    "offered",      # a freed table was offered, awaiting guest reply
    "converted",    # turned into a confirmed reservation
    "expired",      # offer lapsed / guest declined
    "cancelled",
}


class Guest(BaseModel):
    phone: str                      # normalised E.164, e.g. "+923001234567"
    name: str = ""
    vip_tier: str = ""              # "" if not a VIP
    vip_notes: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class Reservation(BaseModel):
    reservation_id: str
    phone: str
    name: str = ""
    party_size: int
    when: datetime                  # reservation start time
    status: str = "confirmed"
    table_id: str = ""
    is_vip: bool = False
    vip_tier: str = ""
    no_show_risk: float = 0.0       # 0..1
    no_show_band: str = "low"       # "low" | "medium" | "high"
    deposit_required: bool = False
    deposit_paid: bool = False
    payment_ref: str = ""           # provider token/id once a deposit link is issued
    reminder_sent: bool = False
    special_requests: str = ""
    source: str = "whatsapp"
    created_at: datetime = Field(default_factory=datetime.now)


class WaitlistEntry(BaseModel):
    waitlist_id: str
    phone: str
    name: str = ""
    party_size: int
    requested_when: datetime
    status: str = "waiting"
    is_vip: bool = False
    vip_tier: str = ""
    offered_at: datetime | None = None
    created_at: datetime = Field(default_factory=datetime.now)
