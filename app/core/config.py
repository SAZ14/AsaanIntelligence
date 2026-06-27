"""Central configuration — reads from .env (all agents combined)."""
from __future__ import annotations
import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

# ── Database ──
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./central.db")

# ── Z.AI (Zhipu AI) — all LLM calls ──
ZAI_API_KEY = os.getenv("ZAI_API_KEY", "")
ZAI_MODEL = os.getenv("ZAI_MODEL", "glm-4.7")

# ── Twilio (internal staff channel — shared across scout/integrity/revenue) ──
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_INTERNAL_FROM = os.getenv("TWILIO_INTERNAL_FROM", "")

# ── Scout-specific ──
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "")

# ── Signature validation ──
TWILIO_VALIDATE_SIGNATURE = os.getenv("TWILIO_VALIDATE_SIGNATURE", "false").lower() == "true"
