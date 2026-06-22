"""Persist short-lived WhatsApp onboarding sessions (awaiting guest name)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services.messaging import normalize_phone

# Abandoned onboarding sessions (guest never replied with a name) expire after this
# many hours so stale "awaiting_name" state doesn't linger and re-greet returning guests.
SESSION_TTL_HOURS = 24


@dataclass
class WhatsAppSession:
    phone: str
    venue_slug: str
    state: str = "awaiting_name"
    updated_at: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_expired(session: WhatsAppSession, ttl_hours: float) -> bool:
    if not session.updated_at:
        return False
    try:
        updated = datetime.fromisoformat(session.updated_at)
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - updated > timedelta(hours=ttl_hours)


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


def get_session(
    path: Path, phone: str, *, ttl_hours: float = SESSION_TTL_HOURS,
) -> WhatsAppSession | None:
    key = normalize_phone(phone)
    session = load_sessions(path).get(key)
    if session is None:
        return None
    if _is_expired(session, ttl_hours):
        clear_session(path, key)
        return None
    return session


def prune_sessions(path: Path, *, ttl_hours: float = SESSION_TTL_HOURS) -> int:
    """Drop all expired sessions in one pass. Returns the number removed."""
    sessions = load_sessions(path)
    expired = [k for k, s in sessions.items() if _is_expired(s, ttl_hours)]
    if not expired:
        return 0
    for k in expired:
        del sessions[k]
    save_sessions(path, sessions)
    return len(expired)


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
