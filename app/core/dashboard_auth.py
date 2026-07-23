"""Staff-dashboard authentication: WhatsApp OTP login + Redis-backed
sessions.

No passwords, no signup flow, no separate credential store -- a phone
proves it's genuinely staff for a store the same way it already does over
WhatsApp: app.core.db.StoreMember is the ONE source of truth for "who can
manage this store", here and in the WhatsApp channel alike. Logging in
just means proving you hold that phone (a one-time code sent to it),
then picking which of your stores to manage if you're on more than one.

Sessions and one-time codes both live in Redis (app.core.cache) --
already the durable cross-instance store this codebase uses everywhere
else, so no new state-storage mechanism is introduced. Redis being
unavailable degrades to "nobody can log in / stay logged in" rather than
a security hole (cache.py's own fail-open-for-reads still leaves
try_lock/get requiring an actual value match to succeed).
"""

from __future__ import annotations

import logging
import re
import secrets

from app.core import cache

logger = logging.getLogger(__name__)

OTP_TTL_SECONDS = 300          # 5 minutes to enter the code
OTP_RESEND_COOLDOWN_SECONDS = 30
SESSION_TTL_SECONDS = 24 * 60 * 60  # 24 hours
SESSION_COOKIE_NAME = "dashboard_session"


def normalize_whatsapp_id(raw_phone: str) -> str:
    """User-entered phone -> the canonical "whatsapp:+<digits>" format
    StoreMember rows are stored in (see app.gateway.main's add_member,
    which always normalizes to this form regardless of which provider
    the store actually sends customer traffic through)."""
    digits = re.sub(r"[^\d+]", "", (raw_phone or "").strip())
    if not digits:
        return ""
    if not digits.startswith("+"):
        digits = "+" + digits.lstrip("+")
    return f"whatsapp:{digits}"


def request_otp(raw_phone: str) -> tuple[bool, str]:
    """Send a one-time code if `raw_phone` is registered staff for at
    least one store. Always returns the SAME generic (True, whatsapp_id)
    shape whether or not the number is actually registered -- the login
    form must not let someone enumerate which phone numbers are staff by
    watching for a different response. Only the actual send (or lack of
    one) differs, invisibly to the caller's HTTP response.

    Returns (True, whatsapp_id) if a code was sent (or would have been,
    for an unregistered number -- caller can't tell), or (False, reason)
    if this number is cooling down from a very recent request."""
    whatsapp_id = normalize_whatsapp_id(raw_phone)
    if not whatsapp_id or whatsapp_id == "whatsapp:":
        return False, "invalid_phone"

    cooldown_key = f"dashboard_otp_cooldown:{whatsapp_id}"
    if not cache.try_lock(cooldown_key, OTP_RESEND_COOLDOWN_SECONDS):
        return False, "cooldown"

    from app.core.db import get_stores_for_number
    stores = get_stores_for_number(whatsapp_id)
    if stores:
        code = f"{secrets.randbelow(1_000_000):06d}"
        cache.set(f"dashboard_otp:{whatsapp_id}", {"code": code}, ttl=OTP_TTL_SECONDS)
        from app.core.outbound import send_from_store
        send_from_store(
            stores[0].id, whatsapp_id,
            f"Your AsaanIntelligence dashboard code is {code}. It expires in 5 minutes.",
        )
        logger.info("dashboard_auth: otp sent to=...%s stores=%d", whatsapp_id[-4:], len(stores))
    else:
        logger.info("dashboard_auth: otp requested for unregistered number ...%s", whatsapp_id[-4:])

    return True, whatsapp_id


def verify_otp(whatsapp_id: str, code: str) -> bool:
    """Single-use: the code is deleted whether or not it matched, so a
    guessed-wrong attempt can't be retried against the same code."""
    entry = cache.get(f"dashboard_otp:{whatsapp_id}")
    cache.delete(f"dashboard_otp:{whatsapp_id}")
    if not entry:
        return False
    return secrets.compare_digest(str(entry.get("code", "")), (code or "").strip())


def create_session(whatsapp_id: str, store_id: int | None = None, role: str | None = None) -> str:
    """A fresh, opaque session token -- the cookie holds only this; the
    actual identity/store/role lives server-side in Redis, so a stolen
    cookie value alone reveals nothing and a session can be revoked
    instantly (delete_session) without any client-side coordination."""
    token = secrets.token_urlsafe(32)
    cache.set(
        f"dashboard_session:{token}",
        {"whatsapp_id": whatsapp_id, "store_id": store_id, "role": role},
        ttl=SESSION_TTL_SECONDS,
    )
    return token


def get_session(token: str) -> dict | None:
    if not token:
        return None
    return cache.get(f"dashboard_session:{token}")


def set_session_store(token: str, store_id: int, role: str) -> None:
    """Called after login when a phone manages more than one store and
    just picked which one -- re-saves the SAME token with store_id/role
    now filled in, rather than issuing a new token (keeps the cookie the
    browser already has valid)."""
    session = get_session(token) or {}
    session["store_id"] = store_id
    session["role"] = role
    cache.set(f"dashboard_session:{token}", session, ttl=SESSION_TTL_SECONDS)


def destroy_session(token: str) -> None:
    if token:
        cache.delete(f"dashboard_session:{token}")
