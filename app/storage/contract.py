"""Executable conformance checks for the `SnapshotStore` contract.

This is the contract in `app/storage/base.py` made runnable. Point
`assert_store_conforms` at a factory that produces a FRESH, EMPTY store and it
exercises the save / latest_before semantics — strictly-before, most-recent-
wins, round-trip fidelity. If a new store passes, it is a guaranteed drop-in
(JsonFileStore today, SupabaseStore once credentials exist).
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from app.models.competitive import (
    Competitor,
    CompetitorMenuItem,
    CompetitorSnapshot,
    ReviewStanding,
    Scope,
)
from app.storage.base import SnapshotStore

_T0 = datetime(2026, 1, 1, 9, 0)   # oldest capture of competitor A
_T1 = datetime(2026, 2, 1, 9, 0)   # newer capture of competitor A
_T2 = datetime(2026, 3, 1, 9, 0)   # the "run" time we diff against


def _snap(cid: str, name: str, captured: datetime, price: float, reviews: int) -> CompetitorSnapshot:
    return CompetitorSnapshot(
        competitor=Competitor(competitor_id=cid, name=name, area="F-7"),
        captured_at=captured,
        menu=[CompetitorMenuItem(name="Latte", category="Coffee", price=price, tags=["x"])],
        reviews=[ReviewStanding(source="Google", rating_avg=4.5, review_count=reviews)],
    )


def assert_store_conforms(make_store: Callable[[], SnapshotStore]) -> None:
    """Assert the store factory yields a contract-conformant store.

    `make_store` MUST return a fresh, empty store on each call (e.g. a new temp
    directory, or a cleaned test table). Raises AssertionError on any violation.
    """
    store = make_store()
    assert isinstance(store, SnapshotStore), (
        f"{type(store).__name__} does not satisfy the SnapshotStore protocol"
    )

    # Empty store: nothing qualifies, must be {} (never None).
    empty = store.latest_before(Scope.LOCAL, _T2)
    assert empty == {}, f"empty store must return {{}}, got {empty!r}"

    # Save two captures of A (history must be preserved) and one of B.
    store.save([_snap("A", "Alpha", _T0, price=100, reviews=10)])
    store.save([_snap("A", "Alpha", _T1, price=120, reviews=50)])
    store.save([_snap("B", "Beta", _T0, price=200, reviews=5)])
    store.save([])  # no-op must not error

    # latest_before(_T2): most-recent-wins for A (T1), and B present.
    got = store.latest_before(Scope.LOCAL, _T2)
    assert set(got) == {"A", "B"}, f"expected keys {{A, B}}, got {set(got)}"
    assert all(isinstance(v, CompetitorSnapshot) for v in got.values()), \
        "values must be CompetitorSnapshot instances"
    assert got["A"].captured_at == _T1, \
        f"most-recent-wins failed: expected A@{_T1}, got {got['A'].captured_at}"

    # Round-trip fidelity on the fields analysis reads.
    a = got["A"]
    assert a.competitor.competitor_id == "A"
    assert a.competitor.name == "Alpha"
    assert a.menu and a.menu[0].name == "Latte" and a.menu[0].price == 120
    assert a.menu[0].tags == ["x"]
    assert a.reviews and a.reviews[0].review_count == 50

    # Strictly-before: at exactly T1, A's T1 capture is excluded -> falls back
    # to T0; a run at T0 excludes everything for A.
    at_t1 = store.latest_before(Scope.LOCAL, _T1)
    assert at_t1["A"].captured_at == _T0, \
        f"strictly-before failed: at {_T1} expected A@{_T0}, got {at_t1['A'].captured_at}"

    at_t0 = store.latest_before(Scope.LOCAL, _T0)
    assert "A" not in at_t0, "strictly-before failed: A@T0 must be excluded at run==T0"
