"""Build menu context string for the customer agent LLM (multi-store)."""
from __future__ import annotations

from urllib.parse import quote


def build_menu_context(store_id: int) -> str:
    from app.agents.customer.community.store import load_deals
    deals = load_deals(store_id)
    lines = []
    active = [d for d in deals if d.active]
    if active:
        lines.append("ACTIVE DEALS:")
        for d in active:
            lines.append(f"- {d.title}: {d.description}")
    return "\n".join(lines) if lines else "No active deals at the moment."


def build_enroll_qr_url(whatsapp_digits: str, greeting: str) -> str:
    return f"https://wa.me/{whatsapp_digits}?text={quote(greeting)}"
