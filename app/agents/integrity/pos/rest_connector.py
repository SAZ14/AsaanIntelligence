"""Generic REST / cloud-POS connector.

Most cloud POS systems (Square, Foodics, Toast, Lightspeed, a custom AsaanPay
POS, …) expose JSON over HTTP. This connector does the shared plumbing — auth,
requests, JSON decode — and normalises records through the same row-level
functions the CSV loader uses, so a flat sales-detail JSON feed works out of the
box with just a field ``mapping``.

Onboarding a POS whose JSON is nested or shaped differently is a small subclass:
override ``_records_to_orders`` / ``_records_to_menu`` / ``_records_to_staff``
(or ``_endpoint`` for auth quirks) — the HTTP layer stays as-is.

Config ``connection`` keys::

    base_url     : e.g. "https://api.somepos.com/v1"   (required)
    endpoints    : {"orders": "...", "menu": "...", "staff": "..."}
    api_key      : bearer token / API key              (optional)
    auth_header  : header name for the key             (default "Authorization")
    auth_scheme  : prefix for the key value            (default "Bearer")
    headers      : extra static headers                (optional)
    timeout      : seconds                             (default 30)

This connector is network-bound; it is exercised in tests via a stub transport
rather than a live endpoint.
"""

from __future__ import annotations

import json
from typing import Any
from urllib import request as urlrequest

from app.ingest import loader
from app.models.canonical import MenuItem, Order, Staff
from app.pos.base import POSConnector, POSData, RestaurantConfig, _load_mapping, register_connector

DEFAULT_ENDPOINTS = {"orders": "orders", "menu": "menu", "staff": "staff"}


class RestPOSConnector(POSConnector):
    def __init__(self, config: RestaurantConfig) -> None:
        super().__init__(config)
        conn = config.connection
        self.base_url = str(conn.get("base_url", "")).rstrip("/")
        if not self.base_url:
            raise ValueError(
                f"{config.venue_name}: REST POS connector requires connection.base_url"
            )
        self.endpoints = {**DEFAULT_ENDPOINTS, **conn.get("endpoints", {})}
        self.api_key = conn.get("api_key")
        self.auth_header = conn.get("auth_header", "Authorization")
        self.auth_scheme = conn.get("auth_scheme", "Bearer")
        self.extra_headers: dict[str, str] = dict(conn.get("headers", {}))
        self.timeout = float(conn.get("timeout", 30))
        self.mapping = _load_mapping(config.mapping)

    # ── HTTP (override _http_get to inject a stub transport in tests) ──

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", **self.extra_headers}
        if self.api_key:
            scheme = f"{self.auth_scheme} " if self.auth_scheme else ""
            headers[self.auth_header] = f"{scheme}{self.api_key}"
        return headers

    def _http_get(self, url: str) -> Any:
        req = urlrequest.Request(url, headers=self._headers())
        with urlrequest.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))

    def _endpoint(self, name: str) -> Any:
        return self._http_get(f"{self.base_url}/{self.endpoints[name].lstrip('/')}")

    # ── Record → canonical (override these for a POS-specific JSON shape) ──

    @staticmethod
    def _records(payload: Any) -> list[dict]:
        """Pull the list of records out of a payload.

        Accepts a bare JSON array or the common ``{"data": [...]}`` envelope.
        """
        if isinstance(payload, dict):
            for key in ("data", "results", "items", "records"):
                if isinstance(payload.get(key), list):
                    return payload[key]
            return []
        return payload if isinstance(payload, list) else []

    def _records_to_orders(self, payload: Any) -> list[Order]:
        return loader.rows_to_orders(self._records(payload), self.mapping.SALES_DETAIL)

    def _records_to_menu(self, payload: Any) -> dict[str, MenuItem]:
        items = (loader.row_to_menu_item(r, self.mapping.MENU) for r in self._records(payload))
        return {item.sku: item for item in items}

    def _records_to_staff(self, payload: Any) -> dict[str, Staff]:
        staff = (loader.row_to_staff(r, self.mapping.STAFF) for r in self._records(payload))
        return {s.staff_id: s for s in staff}

    # ── Public API ──

    def fetch(self) -> POSData:
        return POSData(
            orders=self._records_to_orders(self._endpoint("orders")),
            menu=self._records_to_menu(self._endpoint("menu")),
            staff=self._records_to_staff(self._endpoint("staff")),
        )

    def healthcheck(self) -> bool:
        try:
            self._endpoint("staff")
            return True
        except Exception:
            return False


register_connector("rest", RestPOSConnector)

