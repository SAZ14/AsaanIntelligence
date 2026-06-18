from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models.competitive import CompetitorSnapshot, Scope


@runtime_checkable
class SnapshotStore(Protocol):
    """Append-only-ish store of competitor snapshots over time.

    The contract is intentionally small:
      • `save` records new snapshots (one capture run).
      • `latest_before` returns the most recent snapshot per competitor that
        predates the current run — i.e. the baseline to diff against.
    """

    def save(self, snapshots: list[CompetitorSnapshot]) -> None:
        ...

    def latest_before(
        self,
        scope: Scope,
        run_captured_at,
    ) -> dict[str, CompetitorSnapshot]:
        """Return {competitor_id: most recent snapshot strictly before run}."""
        ...
