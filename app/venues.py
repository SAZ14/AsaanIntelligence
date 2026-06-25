"""Venue registry — DB-driven in production, injectable in tests.

Production: call get_restaurant_config(store_id) to build a RestaurantConfig
from the pos_connections table. No hardcoded venues.

Tests: pass restaurants/owner_map dicts directly to IntegrityWhatsAppService.
"""
from __future__ import annotations

from app.pos import RestaurantConfig

# Keep empty dicts so any code that still imports these doesn't crash.
# Production routing uses get_restaurant_config() instead.
RESTAURANTS: dict[str, RestaurantConfig] = {}
OWNER_WHATSAPP: dict[str, str] = {}
DEFAULT_VENUE: str | None = None


def get_restaurant_config(store_id: int) -> RestaurantConfig | None:
    """Build a RestaurantConfig from the DB pos_connections row for this store."""
    from app.db import SessionLocal, POSConnection, Store
    with SessionLocal() as db:
        store = db.query(Store).filter(Store.id == store_id).first()
        pos = db.query(POSConnection).filter(POSConnection.store_id == store_id).first()
        if not store or not pos:
            return None
        return RestaurantConfig(
            venue_name=store.name,
            pos_type=pos.pos_type,
            connection=dict(pos.config or {}),
            mapping=pos.mapping or "cafe_generic",
            currency=pos.currency or "PKR",
            timezone=pos.timezone or "Asia/Karachi",
        )
