"""Validate inbound Twilio webhook requests via X-Twilio-Signature."""

from __future__ import annotations

import os


def signature_validation_enabled() -> bool:
    """Validate only when explicitly enabled and an auth token is configured.

    Off by default so dev/console mode and the test suite (which can't sign
    requests) keep working without a live Twilio account.
    """
    flag = os.environ.get("ASAAN_VALIDATE_TWILIO_SIGNATURE", "").strip().lower()
    enabled = flag in {"1", "true", "yes", "on"}
    return enabled and bool(os.environ.get("TWILIO_AUTH_TOKEN"))


def is_valid_twilio_request(
    url: str,
    params: dict[str, str],
    signature: str,
    *,
    auth_token: str | None = None,
) -> bool:
    """Return True if `signature` matches Twilio's HMAC over `url` + sorted `params`."""
    from twilio.request_validator import RequestValidator

    token = auth_token or os.environ.get("TWILIO_AUTH_TOKEN", "")
    if not token:
        return False
    validator = RequestValidator(token)
    return validator.validate(url, params, signature or "")
