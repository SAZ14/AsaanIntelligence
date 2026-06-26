"""Store/chain routing helpers shared across all agents."""
from __future__ import annotations
import re
from app.core.db import (
    SessionLocal, Store, StoreMember, UserSession, StoreTwilioNumber,
    get_stores_for_number, get_user_session, set_user_session,
    get_store_by_twilio_number,
)

# ── Agent classification ──────────────────────────────────────────────────────

_SCOUT_KEYWORDS = {
    "scout", "competitor", "competitors", "intel", "intelligence",
    "rivals", "rival", "market", "competition", "landscape",
}
_INTEGRITY_KEYWORDS = {
    "integrity", "audit", "leakage", "leak", "profit", "margin",
    "staff", "void", "theft", "reconcile", "reconciliation",
    "findings", "daily", "weekly", "refresh",
}
_REVENUE_KEYWORDS = {
    "revenue", "sales", "pricing", "price", "strategy", "forecast",
    "trend", "growth", "segment", "analyse", "analyze",
    "upsell", "campaign", "campaigns", "menu", "pricing",
}


def classify_agent(text: str) -> str | None:
    """Return which internal agent should handle this message, or None."""
    words = set(re.sub(r"[^\w\s]", "", text.lower()).split())
    if words & _SCOUT_KEYWORDS:
        return "scout"
    if words & _INTEGRITY_KEYWORDS:
        return "integrity"
    if words & _REVENUE_KEYWORDS:
        return "revenue"
    return None


SWITCH_WORDS = {"switch", "change", "swap", "different", "other"}


def is_switch_intent(text: str) -> bool:
    words = set(re.sub(r"[^\w\s]", "", text.lower()).split())
    return bool(words & SWITCH_WORDS)


# ── Store selection menu ──────────────────────────────────────────────────────

def store_menu(stores: list[Store]) -> str:
    lines = ["Which restaurant would you like to work with?\n"]
    for i, s in enumerate(stores, 1):
        lines.append(f"{i}) {s.name}" + (f" — {s.location}" if s.location else ""))
    lines.append("\nReply with the number.")
    return "\n".join(lines)


def parse_store_selection(text: str, stores: list[Store]) -> Store | None:
    text = text.strip()
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(stores):
            return stores[idx]
    return None
