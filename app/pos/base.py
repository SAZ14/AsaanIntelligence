"""POS connector interface, per-restaurant config, and connector registry.

To onboard a new restaurant / POS:

1. Pick (or write) a connector class that knows how to pull that POS's data and
   normalise it into the canonical models. CSV exports are handled by
   ``CSVPOSConnector``; REST/cloud POS systems by ``RestPOSConnector``.
2. Describe the restaurant with a ``RestaurantConfig`` — which connector to use,
   how to reach it, and which field mapping translates that POS's column/field
   names into ours.
3. Call ``build_connector(config).fetch()`` to get a ``POSData`` bundle.

The mapping dicts in ``app/ingest/mappings`` are reused so a new POS usually
means "copy a mapping, tweak the field names" rather than new ingest code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from app.models.canonical import MenuItem, Order, Staff


@dataclass
class POSData:
    """Everything the integrity engine needs, normalised to canonical models."""

    orders: list[Order] = field(default_factory=list)
    menu: dict[str, MenuItem] = field(default_factory=dict)
    staff: dict[str, Staff] = field(default_factory=dict)


@dataclass
class RestaurantConfig:
    """How to reach one restaurant's POS.

    ``pos_type`` selects the connector (see the registry below). ``connection``
    carries whatever that connector needs — file paths for CSV exports, or
    ``base_url`` / ``api_key`` for a cloud POS. ``mapping`` names the field-map
    module under ``app/ingest/mappings`` (defaults to the generic café map).
    """

    venue_name: str
    pos_type: str = "csv"
    connection: dict[str, Any] = field(default_factory=dict)
    mapping: str = "cafe_generic"
    currency: str = "PKR"
    timezone: str = "Asia/Karachi"


class POSConnector(ABC):
    """Pulls raw data from one POS and returns canonical :class:`POSData`."""

    def __init__(self, config: RestaurantConfig) -> None:
        self.config = config

    @abstractmethod
    def fetch(self) -> POSData:
        """Return all orders, menu, and staff for this restaurant."""

    def healthcheck(self) -> bool:
        """Cheap "can we reach this POS / is config valid?" probe.

        Default implementation attempts a fetch; connectors may override with a
        lighter check (e.g. ping an endpoint).
        """
        try:
            self.fetch()
            return True
        except Exception:
            return False


# ── Connector registry ──
#
# Maps a ``pos_type`` string to a factory. New POS integrations register
# themselves here so configs stay declarative.

_REGISTRY: dict[str, Callable[[RestaurantConfig], POSConnector]] = {}


def register_connector(
    pos_type: str, factory: Callable[[RestaurantConfig], POSConnector]
) -> None:
    _REGISTRY[pos_type] = factory


def build_connector(config: RestaurantConfig) -> POSConnector:
    if config.pos_type not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY)) or "(none registered)"
        raise ValueError(
            f"Unknown pos_type {config.pos_type!r} for venue "
            f"{config.venue_name!r}. Registered types: {known}."
        )
    return _REGISTRY[config.pos_type](config)


def _load_mapping(name: str):
    """Import a field-mapping module from app.ingest.mappings by name."""
    from importlib import import_module

    return import_module(f"app.ingest.mappings.{name}")
