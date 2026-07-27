"""Local LLM client for the loans agent.

Points at any OpenAI-compatible local server — Ollama by default. Once the
model is fine-tuned on the loans dataset (see
``notebooks/finetune_loans_qlora_colab.ipynb``), set ``LOAN_LLM_MODEL`` to the
fine-tuned model tag and ``LOAN_FEWSHOT=0`` — the prompts are already in the
training-data format, so no other change is needed.

Env:
  LOAN_LLM_BASE_URL  default http://localhost:11434/v1  (Ollama)
  LOAN_LLM_MODEL     default llama3.1:8b
  LOAN_LLM_API_KEY   default "ollama" (Ollama ignores it)
  LOAN_FEWSHOT       few-shot examples to prepend (default 4; 0 after fine-tune)
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

FEWSHOT_PATH = Path(__file__).resolve().parents[3] / "data" / "loans" / "fewshot.jsonl"

_client = None


def get_base_url() -> str:
    return os.environ.get("LOAN_LLM_BASE_URL", "http://localhost:11434/v1")


def get_model() -> str:
    return os.environ.get("LOAN_LLM_MODEL", "llama3.1:8b")


def fewshot_count() -> int:
    try:
        return int(os.environ.get("LOAN_FEWSHOT", "4"))
    except ValueError:
        return 4


def get_client():
    """Cached OpenAI-compatible client for the local model."""
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(
            api_key=os.environ.get("LOAN_LLM_API_KEY", "ollama"),
            base_url=get_base_url(),
        )
    return _client


def load_fewshot(tasks: list[str] | None = None, limit: int | None = None) -> list[dict]:
    """Few-shot messages drawn from the curated dataset sample, optionally
    filtered to specific task types."""
    limit = fewshot_count() if limit is None else limit
    if limit <= 0 or not FEWSHOT_PATH.exists():
        return []
    messages: list[dict] = []
    taken = 0
    for line in FEWSHOT_PATH.read_text().splitlines():
        if taken >= limit:
            break
        row = json.loads(line)
        if tasks and row.get("meta", {}).get("task") not in tasks:
            continue
        messages.extend(row["messages"])
        taken += 1
    return messages


def chat(
    user_content: str,
    system: str,
    fewshot_tasks: list[str] | None = None,
    client=None,
    temperature: float = 0.3,
) -> str | None:
    """One chat completion against the local model. Returns None on any
    failure (server down, model missing) so callers can fall back to the
    deterministic narrative."""
    try:
        client = client or get_client()
        messages = [{"role": "system", "content": system}]
        messages += load_fewshot(tasks=fewshot_tasks)
        messages.append({"role": "user", "content": user_content})
        resp = client.chat.completions.create(
            model=get_model(), messages=messages, temperature=temperature,
        )
        return resp.choices[0].message.content
    except Exception as exc:  # noqa: BLE001 — any local-server failure → fallback
        logger.warning("Local LLM unavailable (%s) — using deterministic narrative", exc)
        return None
