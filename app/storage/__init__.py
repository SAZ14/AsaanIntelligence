"""Persistence adapters for competitive snapshots.

`get_store()` returns a Supabase-backed store when credentials are present, and
otherwise a local JSON-file store. Both keep a *history* of snapshots per
competitor — that history is what powers new-dish and review-trend detection.
"""

from app.storage.base import SnapshotStore
from app.storage.jsonstore import JsonFileStore
from app.storage.factory import get_store
from app.storage.contract import assert_store_conforms

__all__ = ["SnapshotStore", "JsonFileStore", "get_store", "assert_store_conforms"]
