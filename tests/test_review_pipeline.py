"""Review scraping pipeline: per-source ok/failed tracking, and confirming
FoodPanda is fully gone (it never produced a single usable review across
any store's history -- no official reviews API, and no Apify actor tried
reliably extracted real review text).
"""
from unittest.mock import patch

import pytest


def test_foodpanda_module_removed():
    with pytest.raises(ModuleNotFoundError):
        import app.review_sources.foodpanda  # noqa: F401


def test_pipeline_never_dispatches_foodpanda():
    import app.review_sources.pipeline as pipeline
    assert not hasattr(pipeline, "fetch_foodpanda")


def test_config_loader_no_longer_returns_foodpanda_keys():
    from app.review_sources.pipeline import _load_config_from_db
    with patch("app.core.db.SessionLocal") as mock_session:
        mock_session.return_value.__enter__.return_value.query.return_value \
            .filter.return_value.first.return_value = None
        cfg = _load_config_from_db(999999)
    assert "foodpanda_url" not in cfg
    assert "foodpanda_keyword" not in cfg


def test_run_pipeline_returns_three_tuple_with_no_store_id():
    from app.review_sources.pipeline import run_pipeline
    reviews, ok, failed = run_pipeline(None)
    assert reviews == []
    assert ok == []
    assert failed == []


def test_run_pipeline_returns_three_tuple_with_no_sources_configured(monkeypatch):
    from app.review_sources import pipeline
    monkeypatch.setattr(pipeline, "_load_config_from_db", lambda store_id: {
        "apify_api_key": "fake-key",
        "google_maps_terms": [],
        "google_maps_location": "",
        "instagram_usernames": [],
    })
    reviews, ok, failed = pipeline.run_pipeline(5)
    assert reviews == []
    assert ok == []
    assert failed == []


def test_run_pipeline_tracks_per_source_success_and_failure(monkeypatch):
    from app.review_sources import pipeline

    monkeypatch.setattr(pipeline, "_load_config_from_db", lambda store_id: {
        "apify_api_key": "fake-key",
        "google_maps_terms": ["Test Cafe"],
        "google_maps_location": "Islamabad",
        "instagram_usernames": ["testcafe"],
    })
    monkeypatch.setattr(pipeline, "fetch_maps",
                        lambda key, terms, loc: [{"source": "Google Maps - Test", "text": "great food"}])
    def _broken_instagram(key, usernames):
        raise RuntimeError("actor timed out")
    monkeypatch.setattr(pipeline, "fetch_instagram", _broken_instagram)

    reviews, ok, failed = pipeline.run_pipeline(5)
    assert ok == ["google_maps"]
    assert failed == ["instagram"]
    assert len(reviews) == 1
    assert reviews[0]["source"] == "Google Maps - Test"


def test_run_pipeline_all_sources_succeed(monkeypatch):
    from app.review_sources import pipeline

    monkeypatch.setattr(pipeline, "_load_config_from_db", lambda store_id: {
        "apify_api_key": "fake-key",
        "google_maps_terms": ["Test Cafe"],
        "google_maps_location": "Islamabad",
        "instagram_usernames": ["testcafe"],
    })
    monkeypatch.setattr(pipeline, "fetch_maps", lambda key, terms, loc: [{"source": "Google Maps"}])
    monkeypatch.setattr(pipeline, "fetch_instagram", lambda key, usernames: [{"source": "Instagram"}])

    reviews, ok, failed = pipeline.run_pipeline(5)
    assert set(ok) == {"google_maps", "instagram"}
    assert failed == []
    assert len(reviews) == 2
