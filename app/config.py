"""Multi-venue configuration.

One install of this tool serves many restaurants. Each venue is just a config
entry pointing at its own folder of CSVs and the owner's WhatsApp number — no
code changes per venue. Twilio account credentials are shared (set once in the
environment); only the recipient differs per venue.

Config lives in `venues.toml` at the repo root, e.g.:

    [[venue]]
    name = "Sugar Rush"
    data_dir = "data"
    owner_whatsapp = "whatsapp:+923001234567"

    [[venue]]
    name = "The Burger Joint"
    data_dir = "venues/burger_joint"
    owner_whatsapp = "whatsapp:+923009998877"
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel


class VenueConfig(BaseModel):
    name: str
    data_dir: str  # folder holding this venue's CSVs (relative to repo root or absolute)
    owner_whatsapp: str = ""  # e.g. "whatsapp:+923001234567"

    def resolve_dir(self, base: Path) -> Path:
        p = Path(self.data_dir)
        return p if p.is_absolute() else base / p


def load_venues(path: Path) -> list[VenueConfig]:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return [VenueConfig(**v) for v in data.get("venue", [])]
