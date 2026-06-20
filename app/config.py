"""Minimal .env loader — no external dependency.

Reads KEY=VALUE lines from a .env file at the repo root and populates
os.environ (without overriding values already set in the environment).
Kept dependency-free so scripts and tests work in a bare container.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env(path: str | Path | None = None, *, override: bool = False) -> None:
    """Load environment variables from a .env file.

    Lines are `KEY=VALUE`. Blank lines and `#` comments are ignored.
    Surrounding single/double quotes on the value are stripped.
    Existing environment variables are preserved unless ``override`` is True.
    Missing file is a no-op.
    """
    env_path = Path(path) if path is not None else REPO_ROOT / ".env"
    if not env_path.exists():
        return

    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = val


def env_flag(name: str, default: bool = False) -> bool:
    """Interpret an env var as a boolean flag."""
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")
