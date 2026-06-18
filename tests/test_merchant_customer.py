"""Tests for Merchant Customer Agent — dashboard, inbox, approval flow."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.merchant_customer import (
    approve_and_send,
    filter_guests,
    run_merchant_customer_agent,
)
from app.agents.customer import run_customer_agent
from app.api.main import app
from app.ingest import load_dataset
from app.ingest.loader import load_customers, load_loyalty_rules, save_loyalty_rules
from app.models.canonical import LineItem, LoyaltyCustomer, LoyaltyRules, MenuItem, Order, Payment, Staff
from app.report.merchant_render import generate_merchant_dashboard

DATA = Path(__file__).resolve().parent.parent / "data"
HOLDOUT = DATA / "holdout"


def _load(data_dir=DATA):
    return load_dataset(
        data_dir / "sales_detail.csv", data_dir / "menu.csv", data_dir / "staff.csv",
    )


def _merchant(data_dir=DATA, outbox=None):
    orders, menu, staff = _load(data_dir)
    registry = load_customers(data_dir / "customers.csv")
    rules = load_loyalty_rules(data_dir / "loyalty_rules.json")
    return run_merchant_customer_agent(
        orders, menu, staff, registry, rules,
        venue_name="Test Café", outbox_path=outbox,
    )


# ── Dashboard tests ──


def test_merchant_dashboard_runs():
    dash = _merchant()
    assert dash.headlines.lapsed_count >= 0
    assert dash.headlines.pending_approval == len(dash.pending_comms)
    assert dash.report is not None


def test_inbox_sorted_critical_first():
    dash = _merchant()
    if len(dash.inbox) >= 2:
        urgencies = [a.urgency for a in dash.inbox]
        order = {"critical": 0, "high": 1, "medium": 2}
        for i in range(len(urgencies) - 1):
            assert order[urgencies[i]] <= order[urgencies[i + 1]]


def test_pending_comms_excludes_opted_out():
    staff = {"S01": Staff(staff_id="S01", name="A", role="server")}
    menu = {"ESP": MenuItem(sku="ESP", name="Espresso", category="Coffee", cost=70, price=420)}
    base = date(2026, 5, 1)
    orders = []
    for i in range(5):
        dt = datetime.combine(base + timedelta(days=i * 3), datetime.min.time().replace(hour=10))
        orders.append(Order(
            order_id=f"O{i}", datetime=dt, staff_id="S01", staff_name="A",
            channel="dine_in", order_status="closed", customer_ref="C_OPT",
            line_items=[LineItem(item_sku="ESP", item_name="Espresso", category="Coffee",
                                 qty=1, unit_price=420, line_amount=420)],
            payments=[Payment(method="card", amount=441, tax_rate=0.05)],
        ))
    end = datetime(2026, 5, 31, 10, 0)
    orders.append(Order(
        order_id="PAD", datetime=end, staff_id="S01", staff_name="A",
        channel="dine_in", order_status="closed", customer_ref="OTHER",
        line_items=[LineItem(item_sku="ESP", item_name="Espresso", category="Coffee",
                             qty=1, unit_price=420, line_amount=420)],
        payments=[Payment(method="card", amount=441, tax_rate=0.05)],
    ))
    registry = {
        "C_OPT": LoyaltyCustomer(
            customer_ref="C_OPT", qr_token="QR-opt", display_name="Opt Out",
            phone="+923001111111", opted_in=False,
        ),
    }
    dash = run_merchant_customer_agent(orders, menu, staff, registry, venue_name="Café")
    assert not any(p.customer_ref == "C_OPT" for p in dash.pending_comms if p.sendable)


def test_filter_guests_by_status():
    dash = _merchant()
    lapsed = filter_guests(dash.guests, status="lapsed")
    for g in lapsed:
        assert g.status == "lapsed"


def test_approve_sends_only_selected_refs(tmp_path):
    dash = _merchant()
    sendable = [p for p in dash.pending_comms if p.sendable]
    if not sendable:
        pytest.skip("No sendable pending comms in dataset")
    ref = sendable[0].customer_ref
    outbox = tmp_path / "outbox.jsonl"
    sent, skipped = approve_and_send(dash, [ref], outbox)
    assert len(sent) == 1
    assert sent[0].customer_ref == ref
    assert outbox.exists()


def test_rules_change_incentive_discount():
    staff = {"S01": Staff(staff_id="S01", name="A", role="server")}
    menu = {"ESP": MenuItem(sku="ESP", name="Espresso", category="Coffee", cost=70, price=420)}
    base = date(2026, 5, 27)
    orders = []
    for i in range(5):
        dt = datetime.combine(base + timedelta(days=i), datetime.min.time().replace(hour=10))
        orders.append(Order(
            order_id=f"O{i}", datetime=dt, staff_id="S01", staff_name="A",
            channel="dine_in", order_status="closed", customer_ref="C_RULE",
            line_items=[LineItem(item_sku="ESP", item_name="Espresso", category="Coffee",
                                 qty=1, unit_price=420, line_amount=420)],
            payments=[Payment(method="card", amount=441, tax_rate=0.05)],
        ))
    end = datetime(2026, 5, 31, 10, 0)
    orders.append(Order(
        order_id="PAD", datetime=end, staff_id="S01", staff_name="A",
        channel="dine_in", order_status="closed", customer_ref="OTHER",
        line_items=[LineItem(item_sku="ESP", item_name="Espresso", category="Coffee",
                             qty=1, unit_price=420, line_amount=420)],
        payments=[Payment(method="card", amount=441, tax_rate=0.05)],
    ))
    registry = {
        "C_RULE": LoyaltyCustomer(
            customer_ref="C_RULE", qr_token="QR-r", display_name="Rule Guest",
            phone="+923002222222", opted_in=True,
        ),
    }
    rules_default = LoyaltyRules()
    rules_custom = LoyaltyRules(milestone_discount_pct=20)
    dash_default = run_merchant_customer_agent(
        orders, menu, staff, registry, rules_default, venue_name="Café",
    )
    dash_custom = run_merchant_customer_agent(
        orders, menu, staff, registry, rules_custom, venue_name="Café",
    )
    mile_default = [p for p in dash_default.pending_comms if p.incentive_type == "visit_milestone"]
    mile_custom = [p for p in dash_custom.pending_comms if p.incentive_type == "visit_milestone"]
    assert mile_default and mile_custom
    assert "10%" in mile_default[0].reward_text or "10" in mile_default[0].reward_text
    assert "20%" in mile_custom[0].reward_text or "20" in mile_custom[0].reward_text


def test_merchant_html_generates():
    dash = _merchant()
    html = generate_merchant_dashboard(dash)
    assert "Merchant Customer Agent" in html
    assert dash.headlines.tagline in html


def test_holdout_merchant_computes():
    dash = _merchant(HOLDOUT)
    assert dash.headlines.period_days > 0
    assert dash.report is not None


# ── API tests ──


@pytest.fixture
def merchant_client(tmp_path, monkeypatch):
    import shutil
    for name in ("sales_detail.csv", "menu.csv", "staff.csv", "customers.csv",
                 "loyalty_rules.json", "venues.json"):
        shutil.copy(DATA / name, tmp_path / name)
    monkeypatch.setenv("ASAAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ASAAN_OUTBOX_DIR", str(tmp_path / "output"))
    return TestClient(app)


def test_merchant_dashboard_api(merchant_client):
    r = merchant_client.get("/merchant/dashboard")
    assert r.status_code == 200
    data = r.json()
    assert "headlines" in data
    assert "pending_comms" in data


def test_merchant_inbox_api(merchant_client):
    r = merchant_client.get("/merchant/inbox")
    assert r.status_code == 200
    assert "inbox" in r.json()


def test_merchant_pending_api(merchant_client):
    r = merchant_client.get("/merchant/incentives/pending")
    assert r.status_code == 200
    data = r.json()
    assert "ready_to_send" in data


def test_merchant_rules_get_put(merchant_client, tmp_path):
    r = merchant_client.get("/merchant/rules")
    assert r.status_code == 200
    r2 = merchant_client.put("/merchant/rules", json={
        "milestone_visit_interval": 5,
        "milestone_discount_pct": 12,
        "winback_lapsed_discount_pct": 15,
        "winback_lapsing_discount_pct": 10,
        "corporate_discount_pct": 5,
        "streak_window_days": 7,
        "streak_min_visits": 3,
        "streak_reward": "complimentary dessert on your next visit",
    })
    assert r2.status_code == 200
    saved = json.loads((tmp_path / "loyalty_rules.json").read_text())
    assert saved["milestone_discount_pct"] == 12


def test_merchant_approve_api(merchant_client):
    pending = merchant_client.get("/merchant/incentives/pending").json()
    sendable = [p["customer_ref"] for p in pending["pending"] if p["sendable"]]
    if not sendable:
        pytest.skip("No sendable pending")
    r = merchant_client.post("/merchant/incentives/approve", json={
        "customer_refs": sendable[:1],
    })
    assert r.status_code == 200
    assert r.json()["sent"] == 1


def test_merchant_opt_out(merchant_client, tmp_path):
    r = merchant_client.put("/merchant/customers/C96285ec529/opt-out")
    assert r.status_code == 200
    registry = load_customers(tmp_path / "customers.csv")
    assert registry["C96285ec529"].opted_in is False


def test_deprecated_dispatch_requires_refs(merchant_client):
    pending = merchant_client.get("/merchant/incentives/pending").json()
    sendable = [p["customer_ref"] for p in pending["pending"] if p["sendable"]]
    if not sendable:
        pytest.skip("No sendable pending")
    r = merchant_client.post("/messages/dispatch", json={"customer_refs": sendable[:1]})
    assert r.status_code == 200
    assert r.json()["deprecated"] is True
