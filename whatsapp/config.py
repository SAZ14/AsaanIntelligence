"""Configuration for the WhatsApp Cloud API integration.

All values come from the environment (a repo-root .env is auto-loaded). Build
against Meta's WhatsApp Cloud API directly:

    https://graph.facebook.com/<version>/<PHONE_NUMBER_ID>/messages
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.config import load_dotenv

# Graph API version the messages endpoint is pinned to.
DEFAULT_GRAPH_VERSION = "v21.0"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class WhatsAppConfig:
    phone_number_id: str = ""
    token: str = ""
    verify_token: str = ""
    graph_version: str = DEFAULT_GRAPH_VERSION
    # Twilio WhatsApp (sandbox or full sender) — the active live-send backend.
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_number: str = ""
    # Validate Twilio's X-Twilio-Signature on inbound webhooks. Off by default
    # so tests/CI don't need a real signature.
    validate_signature: bool = False
    # When DRY_RUN is on we print the exact payload instead of calling out.
    # Defaults to True so nothing is ever sent by accident (and so CI is safe).
    dry_run: bool = True

    @property
    def messages_url(self) -> str:
        return (
            f"https://graph.facebook.com/{self.graph_version}"
            f"/{self.phone_number_id}/messages"
        )

    def require_send_credentials(self) -> None:
        """Raise if we're about to make a real Cloud API call without credentials."""
        missing = [
            name for name, val in (
                ("WHATSAPP_PHONE_NUMBER_ID", self.phone_number_id),
                ("WHATSAPP_TOKEN", self.token),
            ) if not val
        ]
        if missing:
            raise RuntimeError(
                "Cannot send to WhatsApp Cloud API — missing "
                + ", ".join(missing)
                + ". Set them in .env, or set DRY_RUN=true to preview payloads."
            )

    def require_twilio_credentials(self) -> None:
        """Raise if we're about to make a real Twilio call without credentials."""
        missing = [
            name for name, val in (
                ("TWILIO_ACCOUNT_SID", self.twilio_account_sid),
                ("TWILIO_AUTH_TOKEN", self.twilio_auth_token),
                ("TWILIO_WHATSAPP_NUMBER", self.twilio_whatsapp_number),
            ) if not val
        ]
        if missing:
            raise RuntimeError(
                "Cannot send via Twilio — missing "
                + ", ".join(missing)
                + ". Set them in .env, or set DRY_RUN=true to preview payloads."
            )

    @classmethod
    def from_env(cls) -> "WhatsAppConfig":
        load_dotenv()
        return cls(
            phone_number_id=os.environ.get("WHATSAPP_PHONE_NUMBER_ID", ""),
            token=os.environ.get("WHATSAPP_TOKEN", ""),
            verify_token=os.environ.get("WHATSAPP_VERIFY_TOKEN", ""),
            graph_version=os.environ.get("WHATSAPP_GRAPH_VERSION", DEFAULT_GRAPH_VERSION),
            twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID", ""),
            twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN", ""),
            twilio_whatsapp_number=os.environ.get("TWILIO_WHATSAPP_NUMBER", ""),
            validate_signature=_truthy(os.environ.get("TWILIO_VALIDATE_SIGNATURE", "false")),
            dry_run=_truthy(os.environ.get("DRY_RUN", "true")),
        )

