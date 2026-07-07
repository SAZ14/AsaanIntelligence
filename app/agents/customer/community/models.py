from __future__ import annotations
from pydantic import BaseModel, Field


class CommunityMember(BaseModel):
    phone: str
    name: str = ""
    stamps_current: int = 0
    stamps_lifetime: int = 0
    joined_at: str = ""
    last_activity_at: str = ""
    opted_in: bool = True
    winback_sent_at: str = ""


class RedeemCode(BaseModel):
    code: str
    order_id: str = ""
    issued_at: str
    redeemed_at: str = ""
    redeemed_by: str = ""


class StampEvent(BaseModel):
    phone: str
    code: str
    stamp_number: int
    reward_issued: bool = False
    at: str


class Deal(BaseModel):
    title: str
    description: str
    active: bool = True


class VenueConfig(BaseModel):
    venue_name: str = "Restaurant"
    stamp_goal: int = 5
    reward_text: str = "a free drink or dessert"
    winback_days: int = 10
    code_expiry_days: int = 30
    owner_phones: list[str] = Field(default_factory=list)
    qr_greeting: str = ""
