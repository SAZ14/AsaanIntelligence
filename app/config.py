from __future__ import annotations
import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env_integrity")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./integrity.db")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
TWILIO_VALIDATE_SIGNATURE = os.getenv("TWILIO_VALIDATE_SIGNATURE", "false").lower() == "true"
