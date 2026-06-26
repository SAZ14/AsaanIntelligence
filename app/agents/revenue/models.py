"""Pydantic models for state the Revenue agent persists in SQLite:
owner digest subscriptions and a log of recommended campaigns.

Analytics outputs (product performance, pricing recs, dead windows, campaigns)
are plain dataclasses defined alongside the logic in ``analytics``/``pricing`` —
mirroring the existing ``app.analysis`` style.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

CADENCES = {"daily", "weekly", "monthly"}


class OwnerSubscription(BaseModel):
    phone: str                       # normalised owner phone
    name: str = ""
    cadence: str = "weekly"          # daily | weekly | monthly
    hour: int = 9                    # local hour to send the digest
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.now)


class CampaignLogEntry(BaseModel):
    campaign_id: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    window_desc: str = ""            # e.g. "Tuesday Afternoon (14:00–17:00)"
    campaign_name: str = ""          # brand-safe phrasing offered
    target_segment: str = ""
    audience_size: int = 0
    expected_redemptions: int = 0
    est_added_revenue: float = 0.0
    status: str = "suggested"        # suggested | launched | dismissed

