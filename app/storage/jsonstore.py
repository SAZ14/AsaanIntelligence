from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.models.competitive import CompetitorSnapshot, Scope

DEFAULT_STORE_DIR = Path(__file__).resolve().parent.parent.parent / "output" / "competitive_store"


class JsonFileStore:
    """Offline snapshot store: one JSON-lines file of captures on disk.

    Each appended line is a serialised `CompetitorSnapshot`. This is the
    drop-in used when Supabase isn't configured; it preserves enough history
    for the analysis engine to diff consecutive runs.
    """

    def __init__(self, store_dir: Path | str | None = None) -> None:
        self.store_dir = Path(store_dir or DEFAULT_STORE_DIR)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.store_dir / "snapshots.jsonl"

    def save(self, snapshots: list[CompetitorSnapshot]) -> None:
        with open(self.path, "a") as f:
            for s in snapshots:
                f.write(s.model_dump_json() + "\n")

    def _load_all(self) -> list[CompetitorSnapshot]:
        if not self.path.exists():
            return []
        out: list[CompetitorSnapshot] = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if line:
                out.append(CompetitorSnapshot.model_validate_json(line))
        return out

    def latest_before(
        self,
        scope: Scope,
        run_captured_at: datetime,
    ) -> dict[str, CompetitorSnapshot]:
        best: dict[str, CompetitorSnapshot] = {}
        for s in self._load_all():
            if s.captured_at >= run_captured_at:
                continue
            cid = s.competitor.competitor_id
            if cid not in best or s.captured_at > best[cid].captured_at:
                best[cid] = s
        return best
