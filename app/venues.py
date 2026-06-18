"""Central venue registry.

One place that knows every restaurant: how to reach its POS
(:class:`RestaurantConfig`) and which owner WhatsApp number maps to it. Both the
CLI (``scripts/run_integrity_agent.py``) and the WhatsApp service read from here,
so onboarding a venue is a single edit.
"""

from __future__ import annotations

from pathlib import Path

from app.pos import RestaurantConfig

DATA = Path(__file__).resolve().parent.parent / "data"

# venue_key -> how to reach that restaurant's POS.
RESTAURANTS: dict[str, RestaurantConfig] = {
    "roastery": RestaurantConfig(
        venue_name="Roastery (Islamabad)",
        pos_type="csv",
        connection={"base_dir": str(DATA)},
        mapping="cafe_generic",
    ),
    # Onboard a cloud-POS venue (needs real base_url / api_key):
    # "downtown": RestaurantConfig(
    #     venue_name="Downtown Bistro",
    #     pos_type="rest",
    #     connection={"base_url": "https://api.examplepos.com/v1", "api_key": "..."},
    #     mapping="cafe_generic",
    # ),
}

# Owner WhatsApp number (E.164, with the "whatsapp:" prefix Twilio uses)
# -> venue_key. The owner texts the agent; this says which venue they own.
OWNER_WHATSAPP: dict[str, str] = {
    # "whatsapp:+923001234567": "roastery",
}

# Fallback venue when an inbound number is not in OWNER_WHATSAPP (handy for a
# single-venue deployment / demos). Set to None to reject unknown numbers.
DEFAULT_VENUE: str | None = "roastery"
