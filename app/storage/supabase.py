from __future__ import annotations

import os
from datetime import datetime

from app.models.competitive import CompetitorSnapshot, Scope


class SupabaseUnavailable(RuntimeError):
    """Raised when Supabase cannot be used (missing creds / SDK / network)."""


# Suggested schema (run once in the Supabase SQL editor):
#
#   create table competitor_snapshots (
#     id           bigint generated always as identity primary key,
#     competitor_id text not null,
#     scope         text not null,
#     captured_at   timestamptz not null,
#     payload       jsonb not null,
#     created_at    timestamptz default now()
#   );
#   create index on competitor_snapshots (competitor_id, captured_at desc);
#
# We store the whole snapshot as `payload` jsonb so the schema never has to
# chase per-site menu shape changes; queries filter on the promoted columns.

SNAPSHOT_TABLE = "competitor_snapshots"


class SupabaseStore:
    """Snapshot store backed by a Supabase (Postgres) table.

    The SDK and credentials are resolved lazily so importing this module is
    always safe; the factory catches construction failures and falls back to
    the JSON store.
    """

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        table: str = SNAPSHOT_TABLE,
    ) -> None:
        self.url = url or os.environ.get("SUPABASE_URL", "")
        self.key = key or os.environ.get("SUPABASE_KEY", "")
        self.table = table
        if not self.url or not self.key:
            raise SupabaseUnavailable("SUPABASE_URL / SUPABASE_KEY not set")
        try:
            from supabase import create_client
        except ImportError as e:
            raise SupabaseUnavailable(
                "supabase SDK not installed (pip install supabase)"
            ) from e
        self._client = create_client(self.url, self.key)

    def save(self, snapshots: list[CompetitorSnapshot]) -> None:  # pragma: no cover - network
        rows = [
            {
                "competitor_id": s.competitor.competitor_id,
                "scope": s.competitor.country and Scope.LOCAL.value,
                "captured_at": s.captured_at.isoformat(),
                "payload": s.model_dump(mode="json"),
            }
            for s in snapshots
        ]
        if rows:
            self._client.table(self.table).insert(rows).execute()

    def latest_before(
        self,
        scope: Scope,
        run_captured_at: datetime,
    ) -> dict[str, CompetitorSnapshot]:  # pragma: no cover - network
        resp = (
            self._client.table(self.table)
            .select("competitor_id, captured_at, payload")
            .lt("captured_at", run_captured_at.isoformat())
            .order("captured_at", desc=True)
            .execute()
        )
        best: dict[str, CompetitorSnapshot] = {}
        for row in resp.data or []:
            cid = row["competitor_id"]
            if cid not in best:  # rows already ordered newest-first
                best[cid] = CompetitorSnapshot.model_validate(row["payload"])
        return best
