from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from app.community.models import Deal
from app.community.store import load_deals
from app.ingest.loader import load_menu


def build_menu_context(menu_path: Path, deals_path: Path) -> str:
    menu = load_menu(menu_path)
    deals = load_deals(deals_path)
    lines = ["MENU:"]
    by_cat: dict[str, list[str]] = {}
    for item in menu.values():
        by_cat.setdefault(item.category, []).append(f"{item.name} ({item.price:.0f} PKR)")
    for cat in sorted(by_cat.keys()):
        lines.append(f"{cat}: " + ", ".join(by_cat[cat]))
    active = [d for d in deals if d.active]
    if active:
        lines.append("ACTIVE DEALS:")
        for d in active:
            lines.append(f"- {d.title}: {d.description}")
    return "\n".join(lines)


def build_enroll_qr_url(whatsapp_digits: str, greeting: str) -> str:
    return f"https://wa.me/{whatsapp_digits}?text={quote(greeting)}"
