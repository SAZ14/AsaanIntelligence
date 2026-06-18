from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from app.models.competitive import CompetitorSnapshot, Scope


@runtime_checkable
class SnapshotStore(Protocol):
    """FROZEN CONTRACT — the only persistence interface the core depends on.

    A `SnapshotStore` records competitor captures over time and serves the
    baseline to diff against. Every implementation — `JsonFileStore` (offline)
    and `SupabaseStore` (live) — MUST behave identically so the agent and
    `app/analysis/competitive.py` never branch on which backend is in use.
    Swapping one for the other is a drop-in with zero core changes.

    ── Methods ───────────────────────────────────────────────────────────────
    `save(snapshots: list[CompetitorSnapshot]) -> None`
        Persist one capture run. Append semantics: saving does NOT overwrite
        or delete earlier captures of the same competitor — history is what
        powers new-dish and review-momentum detection. Saving an empty list is
        a no-op. Must accept exactly the objects a `Scraper` produces.

    `latest_before(scope: Scope, run_captured_at: datetime)
            -> dict[str, CompetitorSnapshot]`
        Return, per competitor, the single most recent snapshot whose
        `captured_at` is STRICTLY BEFORE `run_captured_at`. The returned dict
        is keyed by `competitor.competitor_id`; each value is a fully-validated
        `CompetitorSnapshot` (same shape the scraper returned). Competitors with
        no qualifying prior snapshot are simply absent from the dict.

    ── Invariants ────────────────────────────────────────────────────────────
      • Strictly-before: a snapshot with `captured_at == run_captured_at` is
        EXCLUDED (so a run never diffs against itself).
      • Most-recent-wins: if several prior snapshots exist for a competitor,
        only the newest is returned.
      • Round-trip fidelity: a snapshot saved and later read back compares equal
        in every field the analysis layer reads (ids, captured_at, menu,
        promotions, reviews).
      • Returns `{}` when nothing qualifies — never `None`.

    `scope` is accepted so a backend may partition storage by tier; the
    contract does not require it to filter results.

    Run `app.storage.contract.assert_store_conforms(...)` against any new
    implementation to verify all of the above.
    """

    def save(self, snapshots: list[CompetitorSnapshot]) -> None:
        ...

    def latest_before(
        self,
        scope: Scope,
        run_captured_at: datetime,
    ) -> dict[str, CompetitorSnapshot]:
        ...
