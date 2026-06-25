"""Pluggable POS connector layer.

Different restaurants run different POS systems. Each one is reached through a
:class:`~app.pos.base.POSConnector` implementation, selected per restaurant via a
:class:`~app.pos.base.RestaurantConfig`. The rest of the app only ever sees the
canonical models (Order / MenuItem / Staff), so the analysis and agent code does
not care which POS the data came from.
"""

from app.pos.base import (
    POSConnector,
    POSData,
    RestaurantConfig,
    build_connector,
    register_connector,
)

# Import built-in connectors for their registration side effects so that
# build_connector() knows about "csv" and "rest" out of the box.
from app.pos import csv_connector as _csv_connector  # noqa: F401,E402
from app.pos import rest_connector as _rest_connector  # noqa: F401,E402

__all__ = [
    "POSConnector",
    "POSData",
    "RestaurantConfig",
    "build_connector",
    "register_connector",
]
