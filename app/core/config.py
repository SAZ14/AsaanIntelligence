"""Central configuration — reads from .env (all agents combined)."""
from __future__ import annotations
import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env")

# ── Database ──
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./central.db")

# ── Anthropic ──
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ── Groq (scout NLU) ──
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# ── Twilio (internal staff channel — shared across scout/integrity/revenue) ──
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_INTERNAL_FROM = os.getenv("TWILIO_INTERNAL_FROM", "")  # one number for all internal agents

# ── Scout-specific ──
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "")

# ── Supabase (customer-agent SDK path) ──
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# ── Signature validation ──
TWILIO_VALIDATE_SIGNATURE = os.getenv("TWILIO_VALIDATE_SIGNATURE", "false").lower() == "true"
