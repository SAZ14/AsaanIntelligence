"""Tiny, dependency-free .env loader.

We deliberately avoid adding python-dotenv as a dependency. This reads a
KEY=VALUE file (ignoring blank lines and `#` comments, tolerating an optional
`export ` prefix and surrounding quotes) and populates os.environ for any key
not already set in the real environment.
"""

from __future__ import annotations

import os
from pathlib import Path

# Repo root = two levels up from this file (app/config.py -> app -> repo root).
REPO_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | str | None = None, *, override: bool = False) -> dict[str, str]:
    """Load a .env file into os.environ. Returns the keys it set.

    Real environment variables win unless override=True. Missing file is a no-op.
    """
    env_path = Path(path) if path is not None else REPO_ROOT / ".env"
    loaded: dict[str, str] = {}
    if not env_path.is_file():
        return loaded

    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded
