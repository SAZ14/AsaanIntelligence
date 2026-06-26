from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, ConfigDict
from datetime import datetime


class FindingSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    competitor_name: str
    source_platform: str   # instagram | website | google_maps | web_search | news
    update_type: str       # new_product | discount | menu_change | campaign | review_trend | event | branch_update | post
    content_text: str
    rating: Optional[float] = None
    post_date: Optional[datetime] = None
    source_url: Optional[str] = None
    image_url: Optional[str] = None
    engagement: Optional[dict] = None   # {likes, comments, shares, review_count}
    ai_summary: Optional[str] = None
    relevance_score: Optional[int] = None
    content_hash: Optional[str] = None

