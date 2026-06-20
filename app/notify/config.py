"""Configuration for outbound WhatsApp notifications.

Settings are read from the process environment, optionally seeded from a local
``.env`` file. No third-party dependency is used for parsing ``.env`` so the
config layer stays importable without Twilio installed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Twilio's shared WhatsApp Sandbox sender. Messages from this number can only
# reach recipients who have explicitly joined the sandbox (`join <code>`), and
# freeform sends outside the 24h session window are rejected.
SANDBOX_SENDER = "whatsapp:+14155238886"

_TRUE = {"1", "true", "yes", "on"}


def load_dotenv(path: str | Path) -> None:
    """Seed ``os.environ`` from a ``.env`` file without overriding real env vars.

    Lines are ``KEY=VALUE``; blanks and ``#`` comments are ignored. Surrounding
    single/double quotes on the value are stripped. Existing environment
    variables always win (``setdefault`` semantics).
    """
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _normalize_whatsapp(number: str) -> str:
    """Ensure a number carries the ``whatsapp:`` channel prefix Twilio expects."""
    n = number.strip()
    if not n:
        return n
    if n.startswith("whatsapp:"):
        return n
    return f"whatsapp:{n}"


def _first(env: dict[str, str], *names: str) -> str:
    for name in names:
        val = env.get(name)
        if val:
            return val
    return ""


class ConfigError(ValueError):
    """Raised when required WhatsApp settings are missing or malformed."""


@dataclass(frozen=True)
class WhatsAppSettings:
    account_sid: str
    auth_token: str
    sender: str
    recipient: str
    dry_run: bool

    @property
    def is_sandbox(self) -> bool:
        return self.sender == SANDBOX_SENDER

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        dotenv_path: str | Path | None = None,
    ) -> "WhatsAppSettings":
        """Build settings from the environment.

        Accepts both the canonical names (``TWILIO_WHATSAPP_NUMBER``,
        ``OWNER_NUMBER``) and the FROM/TO aliases for convenience.
        """
        if dotenv_path is not None:
            load_dotenv(dotenv_path)
        env = dict(os.environ if env is None else env)

        sid = _first(env, "TWILIO_ACCOUNT_SID")
        token = _first(env, "TWILIO_AUTH_TOKEN")
        sender = _first(env, "TWILIO_WHATSAPP_NUMBER", "TWILIO_WHATSAPP_FROM")
        recipient = _first(env, "OWNER_NUMBER", "TWILIO_WHATSAPP_TO")
        dry_run = env.get("DRY_RUN", "0").strip().lower() in _TRUE

        missing = [
            name
            for name, val in (
                ("TWILIO_ACCOUNT_SID", sid),
                ("TWILIO_AUTH_TOKEN", token),
                ("TWILIO_WHATSAPP_NUMBER / TWILIO_WHATSAPP_FROM", sender),
                ("OWNER_NUMBER / TWILIO_WHATSAPP_TO", recipient),
            )
            if not val
        ]
        if missing:
            raise ConfigError("Missing required settings: " + ", ".join(missing))

        if not sid.startswith("AC"):
            raise ConfigError("TWILIO_ACCOUNT_SID should start with 'AC'")

        return cls(
            account_sid=sid,
            auth_token=token,
            sender=_normalize_whatsapp(sender),
            recipient=_normalize_whatsapp(recipient),
            dry_run=dry_run,
        )
