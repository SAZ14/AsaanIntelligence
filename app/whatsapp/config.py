from __future__ import annotations

import os
from dataclasses import dataclass

from app.config import load_dotenv

DEFAULT_GRAPH_VERSION = "v21.0"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class WhatsAppConfig:
    phone_number_id: str = ""
    token: str = ""
    verify_token: str = ""
    graph_version: str = DEFAULT_GRAPH_VERSION
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_number: str = ""
    validate_signature: bool = False
    dry_run: bool = True

    @property
    def messages_url(self) -> str:
        return (
            f"https://graph.facebook.com/{self.graph_version}"
            f"/{self.phone_number_id}/messages"
        )

    def require_send_credentials(self) -> None:
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
        twilio_num = os.environ.get("TWILIO_WHATSAPP_NUMBER") or os.environ.get("TWILIO_WHATSAPP_MERCHANT_FROM") or ""
        return cls(
            phone_number_id=os.environ.get("WHATSAPP_PHONE_NUMBER_ID", ""),
            token=os.environ.get("WHATSAPP_TOKEN", ""),
            verify_token=os.environ.get("WHATSAPP_VERIFY_TOKEN", ""),
            graph_version=os.environ.get("WHATSAPP_GRAPH_VERSION", DEFAULT_GRAPH_VERSION),
            twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID", ""),
            twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN", ""),
            twilio_whatsapp_number=twilio_num,
            validate_signature=_truthy(os.environ.get("TWILIO_VALIDATE_SIGNATURE", "false")),
            dry_run=_truthy(os.environ.get("DRY_RUN", "true")),
        )
