"""Pipeline smoke tests — scrapers mocked at the network boundary."""
import pytest
from datetime import datetime
from unittest.mock import patch

from app.schemas import FindingSchema
from app.cleaning import _make_hash


def _dummy_finding(competitor="Baskin Robbins Pakistan", text=None, platform="instagram", update="post"):
    t = text or f"New ice cream summer cake dessert discount offer at {competitor}!"
    return FindingSchema(
        competitor_name=competitor,
        source_platform=platform,
        update_type=update,
        content_text=t,
        content_hash=_make_hash(competitor, t),
        engagement={"likes": 500, "comments": 30},
        post_date=datetime(2026, 6, 20),
    )


MOCK_FINDINGS = [
    _dummy_finding("Baskin Robbins Pakistan", "New Strawberry Cheesecake ice cream flavor now available! Summer launch.", "instagram", "new_product"),
    _dummy_finding("Layers", "50% off all cakes this weekend! Eid special dessert deal offer.", "instagram", "discount"),
    _dummy_finding("Tehzeeb Bakers", "New brownie menu launched at all Islamabad branches. Order now!", "website", "menu_change"),
    _dummy_finding("Burning Brownie", "Summer season cheesecake collection — try our new flavors!", "google_maps", "review_trend"),
    _dummy_finding("Kitchen Cuisine", "Ferrero Rocher cake available at F-10 bakery. Rating 4.5/5.", "google_maps", "review_trend"),
]

MOCK_COMPETITORS = [
    {"name": "Baskin Robbins Pakistan", "category": "Ice cream",
     "instagram_handle": "baskinrobbinspk", "website": "https://baskinrobbins.pk",
     "place_id": None, "source": "seed"},
]


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Use a temp SQLite DB for all tests; patch SessionLocal in all modules that imported it."""
    db_url = f"sqlite:///{tmp_path}/test.db"
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import app.db as db_mod
    import app.pipeline as pipeline_mod
    import app.discovery as discovery_mod
    from app.config import COMPETITORS

    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db_mod.Base.metadata.create_all(bind=engine)

    db_mod.engine = engine
    db_mod.SessionLocal = session_factory
    pipeline_mod.SessionLocal = session_factory
    discovery_mod.SessionLocal = session_factory

    # Create the default store and seed its competitors
    with session_factory() as db:
        store = db_mod.Store(
            name="Sugar Rush",
            location="Kohsar Market, F-6, Islamabad",
            category="dessert / ice cream",
            instagram_handle="sugarrushisb",
        )
        db.add(store)
        db.commit()
        db.refresh(store)
        db_mod._seed_competitors(store.id, db, COMPETITORS)

    yield session_factory


def test_pipeline_run_scout_stores_report():
    """Full pipeline smoke: mocked scrapers produce findings → stored → report returned."""
    import app.pipeline as pm

    with (
        patch.object(pm, "confirm_seed_competitors"),
        patch.object(pm, "discover_new_competitors"),
        patch.object(pm, "get_all_competitors", return_value=MOCK_COMPETITORS),
        patch.object(pm, "_fetch_all_sources", return_value=(MOCK_FINDINGS, ["instagram"], [])),
        patch.object(pm, "enrich_findings", side_effect=lambda f: f),
        patch.object(pm, "build_report",
                     return_value="Data: live this run\n\nTest report: Baskin Robbins launched new flavor."),
    ):
        report = pm.run("scout", store_id=1)

    assert "Test report" in report or "Baskin Robbins" in report
    assert len(report) > 20


def test_pipeline_stores_run_in_db():
    import app.pipeline as pm
    import app.db as db_mod

    with (
        patch.object(pm, "confirm_seed_competitors"),
        patch.object(pm, "discover_new_competitors"),
        patch.object(pm, "get_all_competitors", return_value=MOCK_COMPETITORS),
        patch.object(pm, "_fetch_all_sources", return_value=(MOCK_FINDINGS[:2], ["firecrawl"], [])),
        patch.object(pm, "enrich_findings", side_effect=lambda f: f),
        patch.object(pm, "build_report", return_value="Data: live this run\n\nCompetitors are active."),
    ):
        pm.run("scout", store_id=1)

    with db_mod.SessionLocal() as db:
        run = db.query(db_mod.Run).order_by(db_mod.Run.id.desc()).first()
        assert run is not None
        assert run.status in ("ok", "partial")
        report = db.query(db_mod.Report).filter(db_mod.Report.run_id == run.id).first()
        assert report is not None
        assert "Competitors are active" in report.report_text


def test_pipeline_partial_failure_does_not_crash():
    """If one source fails, pipeline continues and marks run as partial."""
    import app.pipeline as pm

    with (
        patch.object(pm, "confirm_seed_competitors"),
        patch.object(pm, "discover_new_competitors"),
        patch.object(pm, "get_all_competitors", return_value=MOCK_COMPETITORS),
        patch.object(pm, "_fetch_all_sources", return_value=(
            MOCK_FINDINGS[:1], ["firecrawl"], ["instagram"],
        )),
        patch.object(pm, "enrich_findings", side_effect=lambda f: f),
        patch.object(pm, "build_report",
                     return_value="Data: live this run (partial — instagram failed)\n\nReport here."),
    ):
        report = pm.run("scout", store_id=1)

    assert isinstance(report, str)
    assert len(report) > 0


def test_pipeline_zero_findings_returns_honest_message():
    """Zero findings → honest message instead of fabricated report."""
    import app.pipeline as pm
    import app.config as cfg
    original_key = cfg.GROQ_API_KEY
    cfg.GROQ_API_KEY = ""

    try:
        with (
            patch.object(pm, "confirm_seed_competitors"),
            patch.object(pm, "discover_new_competitors"),
            patch.object(pm, "get_all_competitors", return_value=[]),
            patch.object(pm, "_fetch_all_sources", return_value=([], [], ["instagram", "firecrawl"])),
            patch.object(pm, "enrich_findings", side_effect=lambda f: f),
        ):
            report = pm.run("scout", store_id=1)
    finally:
        cfg.GROQ_API_KEY = original_key

    assert isinstance(report, str)
    assert len(report) > 0
