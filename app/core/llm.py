"""Shared Z.AI (Zhipu AI) LLM client.

Every LLM call in this codebase goes through one client so switching providers
only requires changing ZAI_API_KEY and ZAI_MODEL in the environment.
"""
from __future__ import annotations
import os

ZAI_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
_DEFAULT_MODEL = "glm-4.7"

_client = None


def get_client():
    """Return a cached Z.AI OpenAI-compatible client. Raises if key is missing."""
    global _client
    if _client is None:
        from openai import OpenAI
        key = os.environ.get("ZAI_API_KEY", "")
        if not key:
            raise RuntimeError("ZAI_API_KEY not set")
        _client = OpenAI(api_key=key, base_url=ZAI_BASE_URL)
    return _client


def get_model() -> str:
    return os.environ.get("ZAI_MODEL", _DEFAULT_MODEL)


def get_customer_model() -> str:
    """Model used by the customer-facing chat agent — faster/cheaper Flash by default."""
    return os.environ.get("CUSTOMER_ZAI_MODEL", get_model())


def get_fast_model() -> str:
    """Model for latency-critical, low-complexity calls: message routing,
    response rewriting, intent classification. Non-reasoning by default.
    Measured on the live API: the router call is 0.6-0.8s here vs 9.6s on
    glm-4.7 with thinking, at identical accuracy on the routing eval set."""
    return os.environ.get("FAST_ZAI_MODEL", get_customer_model())


# GLM hybrid-reasoning models accept a thinking toggle on the OpenAI-compat
# endpoint; other models may reject the parameter, hence the prefix gate.
_HYBRID_REASONING_PREFIXES = ("glm-4.5", "glm-4.6", "glm-4.7")


def nothink_kwargs(model: str) -> dict:
    """chat.completions kwargs that disable chain-of-thought for interactive
    calls. Measured live: a typical staff reply on glm-4.7 drops from 17.7s
    to 3.3s with equivalent output quality. Returns {} for models without
    a thinking mode."""
    if model.startswith(_HYBRID_REASONING_PREFIXES):
        return {"extra_body": {"thinking": {"type": "disabled"}}}
    return {}
