from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class WhatsAppConfig:
    account_sid: str
    auth_token: str
    from_number: str
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "WhatsAppConfig":
        return cls(
            account_sid=os.environ.get("TWILIO_ACCOUNT_SID", ""),
            auth_token=os.environ.get("TWILIO_AUTH_TOKEN", ""),
            from_number=(
                os.environ.get("TWILIO_WHATSAPP_MERCHANT_FROM")
                or os.environ.get("TWILIO_WHATSAPP_NUMBER", "")
            ),
            dry_run=os.environ.get("DRY_RUN", "false").lower() == "true",
        )

    def is_valid(self) -> bool:
        return bool(self.account_sid and self.auth_token and self.from_number)
