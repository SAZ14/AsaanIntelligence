from pathlib import Path

import pytest

from app.pos import RestaurantConfig, build_connector
from app.pos.rest_connector import RestPOSConnector

DATA = Path(__file__).resolve().parent.parent / "data"


def _csv_config():
    return RestaurantConfig(
        venue_name="Test Café", pos_type="csv",
        connection={"base_dir": str(DATA)}, mapping="cafe_generic",
    )


def test_csv_connector_fetches_canonical_data():
    data = build_connector(_csv_config()).fetch()
    assert 3700 <= len(data.orders) <= 3900
    assert len(data.menu) > 0
    assert len(data.staff) == 8
    # canonical shape preserved
    assert data.orders[0].payments[0].amount > 0


def test_csv_connector_healthcheck():
    assert build_connector(_csv_config()).healthcheck() is True
    bad = RestaurantConfig(
        venue_name="Missing", pos_type="csv",
        connection={"base_dir": "/no/such/dir"},
    )
    assert build_connector(bad).healthcheck() is False


def test_unknown_pos_type_raises():
    cfg = RestaurantConfig(venue_name="X", pos_type="nope")
    with pytest.raises(ValueError, match="Unknown pos_type"):
        build_connector(cfg)


def test_rest_connector_requires_base_url():
    cfg = RestaurantConfig(venue_name="X", pos_type="rest", connection={})
    with pytest.raises(ValueError, match="base_url"):
        build_connector(cfg)


class _StubRestConnector(RestPOSConnector):
    """REST connector with a canned transport instead of real HTTP."""

    PAYLOADS = {
        "staff": [{"staff_id": "S1", "name": "Sam", "role": "server"}],
        "menu": [{"sku": "ESP", "name": "Espresso", "category": "Coffee",
                  "cost": "70", "price": "420"}],
        "orders": {"data": [{
            "order_id": "O1", "datetime": "2026-05-01 10:00:00", "staff_id": "S1",
            "staff_name": "Sam", "table": "", "channel": "dine_in",
            "item_sku": "ESP", "item_name": "Espresso", "category": "Coffee",
            "qty": "1", "unit_price": "420", "line_amount": "420", "discount_amount": "0",
            "is_void": "0", "void_after_fire": "0", "is_comp": "0",
            "order_status": "closed", "payment_method": "card",
            "payment_amount": "441", "tax_rate": "0.05", "customer_ref": "C1",
        }]},
    }

    def _http_get(self, url: str):
        for name, ep in self.endpoints.items():
            if url.endswith(ep):
                return self.PAYLOADS[name]
        raise AssertionError(f"unexpected url {url}")


def test_rest_connector_normalizes_json():
    cfg = RestaurantConfig(
        venue_name="Cloud POS", pos_type="rest",
        connection={"base_url": "https://api.example.com/v1", "api_key": "k"},
    )
    data = _StubRestConnector(cfg).fetch()
    assert len(data.orders) == 1
    assert data.orders[0].order_id == "O1"
    assert data.orders[0].line_items[0].item_sku == "ESP"
    assert data.menu["ESP"].price == 420
    assert data.staff["S1"].name == "Sam"


def test_rest_connector_auth_header():
    cfg = RestaurantConfig(
        venue_name="Cloud POS", pos_type="rest",
        connection={"base_url": "https://x", "api_key": "secret"},
    )
    headers = _StubRestConnector(cfg)._headers()
    assert headers["Authorization"] == "Bearer secret"
