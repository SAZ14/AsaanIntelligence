"""Receipt code generation and validation (multi-store)."""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

from app.agents.customer.community.models import RedeemCode, VenueConfig
from app.agents.customer.community.store import (
    append_redeem_code, load_redeem_codes, update_redeem_code,
)

CODE_PATTERN = re.compile(r"^SR-[A-Z0-9]{4}$", re.IGNORECASE)


def normalize_code(raw: str) -> str:
    return raw.strip().upper()


def is_redeem_code(text: str) -> bool:
    return bool(CODE_PATTERN.match(normalize_code(text)))


def issue_redeem_code(store_id: int, *, order_id: str = "") -> RedeemCode:
    existing = {c.code for c in load_redeem_codes(store_id)}
    for _ in range(100):
        suffix = secrets.token_hex(2).upper()
        code = f"SR-{suffix}"
        if code not in existing:
            entry = RedeemCode(
                code=code,
                order_id=order_id,
                issued_at=datetime.now(timezone.utc).isoformat(),
            )
            append_redeem_code(store_id, entry)
            return entry
    raise RuntimeError("Could not generate unique redeem code")


def find_code(store_id: int, code: str) -> RedeemCode | None:
    normalized = normalize_code(code)
    for entry in load_redeem_codes(store_id):
        if entry.code == normalized:
            return entry
    return None


def validate_code(entry: RedeemCode, config: VenueConfig) -> str | None:
    if entry.redeemed_at:
        return "This code has already been used."
    issued = datetime.fromisoformat(entry.issued_at)
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - issued > timedelta(days=config.code_expiry_days):
        return "This code has expired. Get a fresh one at the counter!"
    return None


def mark_redeemed(store_id: int, entry: RedeemCode, phone: str) -> RedeemCode:
    updated = entry.model_copy(update={
        "redeemed_at": datetime.now(timezone.utc).isoformat(),
        "redeemed_by": phone,
    })
    update_redeem_code(store_id, updated)
    return updated
