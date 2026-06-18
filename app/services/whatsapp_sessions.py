"""Persist short-lived WhatsApp onboarding sessions (awaiting guest name)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.services.messaging import normalize_phone


@dataclass
class WhatsAppSession:
    phone: str
    venue_slug: str
    state: str = "awaiting_name"
    updated_at: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_sessions(path: Path) -> dict[str, WhatsAppSession]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    sessions: dict[str, WhatsAppSession] = {}
    for phone, data in raw.items():
        sessions[phone] = WhatsAppSession(**data)
    return sessions


def save_sessions(path: Path, sessions: dict[str, WhatsAppSession]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {phone: asdict(session) for phone, session in sessions.items()}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def get_session(path: Path, phone: str) -> WhatsAppSession | None:
    key = normalize_phone(phone)
    return load_sessions(path).get(key)


def set_awaiting_name(path: Path, phone: str, venue_slug: str) -> WhatsAppSession:
    key = normalize_phone(phone)
    sessions = load_sessions(path)
    session = WhatsAppSession(
        phone=key,
        venue_slug=venue_slug,
        state="awaiting_name",
        updated_at=_now(),
    )
    sessions[key] = session
    save_sessions(path, sessions)
    return session


def clear_session(path: Path, phone: str) -> None:
    key = normalize_phone(phone)
    sessions = load_sessions(path)
    if key in sessions:
        del sessions[key]
        save_sessions(path, sessions)
