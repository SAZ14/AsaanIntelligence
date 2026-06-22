"""Shared API dependencies — data paths and loaders."""

from __future__ import annotations

import os
from pathlib import Path

from app.ingest import load_dataset
from app.ingest.loader import load_customers, load_loyalty_rules, save_customers, save_loyalty_rules
from app.models.canonical import LoyaltyCustomer, LoyaltyRules
from app.services.messaging import (
    DEFAULT_OUTBOX_BACKUP_COUNT,
    DEFAULT_OUTBOX_MAX_BYTES,
    ConsoleMessageDispatcher,
    FileOutboxDispatcher,
    get_dispatcher,
)

VENUE_NAME = os.environ.get("ASAAN_VENUE_NAME", "Sugar Rush")
VENUE_SLUG = os.environ.get("ASAAN_VENUE_SLUG", "sugar-rush")
JOIN_BASE_URL = os.environ.get("ASAAN_JOIN_BASE_URL", "http://localhost:8000")


def public_base_url() -> str:
    """Public URL Twilio posts to — used to validate inbound webhook signatures."""
    return os.environ.get("ASAAN_PUBLIC_BASE_URL", "").rstrip("/")


def data_dir() -> Path:
    return Path(os.environ.get(
        "ASAAN_DATA_DIR",
        Path(__file__).resolve().parent.parent.parent / "data",
    ))


def outbox_dir() -> Path:
    return Path(os.environ.get(
        "ASAAN_OUTBOX_DIR",
        Path(__file__).resolve().parent.parent.parent / "output",
    ))


def registry_path() -> Path:
    return data_dir() / "customers.csv"


def rules_path() -> Path:
    return data_dir() / "loyalty_rules.json"


def venues_path() -> Path:
    return data_dir() / "venues.json"


def outbox_path() -> Path:
    return outbox_dir() / "messages_outbox.jsonl"


def sessions_path() -> Path:
    return data_dir() / "whatsapp_sessions.json"


def get_message_dispatcher():
    """Twilio WhatsApp when credentials set, else console; always logged to outbox."""
    inner = get_dispatcher()
    return FileOutboxDispatcher(
        outbox_path(), inner,
        max_bytes=DEFAULT_OUTBOX_MAX_BYTES,
        backup_count=DEFAULT_OUTBOX_BACKUP_COUNT,
    )


def load_registry() -> dict[str, LoyaltyCustomer]:
    return load_customers(registry_path())


def load_rules() -> LoyaltyRules:
    return load_loyalty_rules(rules_path())


def save_rules(rules: LoyaltyRules) -> None:
    save_loyalty_rules(rules_path(), rules)


def load_orders():
    d = data_dir()
    return load_dataset(
        d / "sales_detail.csv",
        d / "menu.csv",
        d / "staff.csv",
    )


def build_merchant_dashboard():
    from app.agents.merchant_customer import run_merchant_customer_agent

    orders, menu, staff = load_orders()
    registry = load_registry()
    rules = load_rules()
    return run_merchant_customer_agent(
        orders, menu, staff, registry, rules,
        venue_name=VENUE_NAME,
        outbox_path=outbox_path(),
    )
