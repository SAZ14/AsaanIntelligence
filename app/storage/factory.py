from __future__ import annotations

import os
import sys

from app.storage.base import SnapshotStore
from app.storage.jsonstore import JsonFileStore


def get_store(force_local: bool = False) -> SnapshotStore:
    """Return the best available snapshot store for the environment.

    Prefers Supabase when credentials are present; otherwise (or on any
    construction error) falls back to the local JSON-file store.
    """
    if force_local:
        return JsonFileStore()

    has_creds = bool(os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_KEY"))
    if not has_creds:
        return JsonFileStore()

    try:
        from app.storage.supabase import SupabaseStore

        return SupabaseStore()
    except Exception as e:  # pragma: no cover - depends on environment
        print(f"[storage] Supabase unavailable ({e}); using local JSON store", file=sys.stderr)
        return JsonFileStore()
