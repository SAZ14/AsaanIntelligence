"""Tests for send-cooldown dedup in approve_and_send (no double-sends on re-approval)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.merchant_customer import approve_and_send, run_merchant_customer_agent
from app.ingest import load_dataset
from app.ingest.loader import load_customers, load_loyalty_rules

DATA = Path(__file__).resolve().parent.parent / "data"


def _merchant():
    orders, menu, staff = load_dataset(
        DATA / "sales_detail.csv", DATA / "menu.csv", DATA / "staff.csv",
    )
    registry = load_customers(DATA / "customers.csv")
    rules = load_loyalty_rules(DATA / "loyalty_rules.json")
    return run_merchant_customer_agent(
        orders, menu, staff, registry, rules,
        venue_name="Test Café",
    )


def _first_sendable_ref(dash):
    sendable = [p for p in dash.pending_comms if p.sendable]
    if not sendable:
        pytest.skip("No sendable pending comms in dataset")
    return sendable[0].customer_ref


def test_repeat_approval_skipped_within_cooldown(tmp_path):
    dash = _merchant()
    ref = _first_sendable_ref(dash)
    outbox = tmp_path / "outbox.jsonl"

    sent1, _ = approve_and_send(dash, [ref], outbox)
    assert len(sent1) == 1

    sent2, skipped2 = approve_and_send(dash, [ref], outbox)
    assert sent2 == []
    assert any(
        p.customer_ref == ref and p.block_reason == "recently messaged"
        for p in skipped2
    )


def test_cooldown_zero_allows_resend(tmp_path):
    dash = _merchant()
    ref = _first_sendable_ref(dash)
    outbox = tmp_path / "outbox.jsonl"

    sent1, _ = approve_and_send(dash, [ref], outbox, cooldown_days=0)
    sent2, _ = approve_and_send(dash, [ref], outbox, cooldown_days=0)
    assert len(sent1) == 1
    assert len(sent2) == 1
