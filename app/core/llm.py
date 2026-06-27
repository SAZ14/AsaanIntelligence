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
