"""Review scraping pipeline: per-source ok/failed tracking, and confirming
FoodPanda is fully gone (it never produced a single usable review across
any store's history -- no official reviews API, and no Apify actor tried
reliably extracted real review text).
"""
from unittest.mock import patch, MagicMock

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
        "store_name": "Test Cafe",
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
        "store_name": "Test Cafe",
    })
    monkeypatch.setattr(pipeline, "fetch_maps",
                        lambda key, terms, loc, name: [{"source": "Google Maps - Test", "text": "great food"}])
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
        "store_name": "Test Cafe",
    })
    monkeypatch.setattr(pipeline, "fetch_maps", lambda key, terms, loc, name: [{"source": "Google Maps"}])
    monkeypatch.setattr(pipeline, "fetch_instagram", lambda key, usernames: [{"source": "Instagram"}])

    reviews, ok, failed = pipeline.run_pipeline(5)
    assert set(ok) == {"google_maps", "instagram"}
    assert failed == []
    assert len(reviews) == 2


# ── Instagram: only comments are customer feedback, captions are not ────────
#
# Post captions are the business's own marketing copy, not customer
# sentiment. Treating them as "reviews" made no sense on its own, and
# combined with the (separately fixed) rating=0-as-negative bug, an old
# caption could surface as a "pending negative review" needing a reply --
# to the business's own post.

class TestInstagramCaptionsExcluded:
    def test_captions_never_become_findings(self, monkeypatch):
        from app.review_sources import instagram as ig

        posts = [{
            "url": "https://instagram.com/p/abc123",
            "caption": "New burger launch this week! 🍔",
            "ownerUsername": "anatummyisb",
            "timestamp": "2026-07-01T00:00:00Z",
        }]
        monkeypatch.setattr(ig, "_fetch_posts", lambda client, usernames: posts)
        monkeypatch.setattr(ig, "_fetch_comments", lambda client, urls: [])

        with patch("apify_client.ApifyClient"):
            result = ig.fetch_reviews("fake-key", ["anatummyisb"])

        assert result == []
        assert not any("burger launch" in item.get("text", "") for item in result)

    def test_comments_still_become_findings(self, monkeypatch):
        from app.review_sources import instagram as ig

        posts = [{"url": "https://instagram.com/p/abc123", "caption": "New burger!",
                  "ownerUsername": "anatummyisb", "timestamp": "2026-07-01T00:00:00Z"}]
        comments = [{
            "text": "This was amazing, best burger in town!",
            "parentPostUrl": "https://instagram.com/p/abc123",
            "ownerUsername": "a_real_customer",
            "timestamp": "2026-07-02T00:00:00Z",
        }]
        monkeypatch.setattr(ig, "_fetch_posts", lambda client, usernames: posts)
        monkeypatch.setattr(ig, "_fetch_comments", lambda client, urls: comments)

        with patch("apify_client.ApifyClient"):
            result = ig.fetch_reviews("fake-key", ["anatummyisb"])

        assert len(result) == 1
        assert result[0]["text"] == "This was amazing, best burger in town!"
        assert result[0]["author"] == "a_real_customer"

    def test_posts_still_fetched_to_discover_comment_urls(self, monkeypatch):
        """Posts must still be pulled -- comments are fetched per-post-URL,
        so removing caption-as-finding shouldn't also stop post discovery."""
        from app.review_sources import instagram as ig
        calls = []

        def _fake_fetch_posts(client, usernames):
            calls.append("posts")
            return [{"url": "https://instagram.com/p/xyz", "caption": "hi"}]

        def _fake_fetch_comments(client, urls):
            calls.append(("comments", urls))
            return []

        monkeypatch.setattr(ig, "_fetch_posts", _fake_fetch_posts)
        monkeypatch.setattr(ig, "_fetch_comments", _fake_fetch_comments)

        with patch("apify_client.ApifyClient"):
            ig.fetch_reviews("fake-key", ["anatummyisb"])

        assert "posts" in calls
        assert ("comments", ["https://instagram.com/p/xyz"]) in calls


# ── Google Maps: business-name filter excludes unrelated places ─────────────
#
# Confirmed live: with maxCrawledPlacesPerSearch=5 and no name validation,
# Google's search for an ambiguous term like "Anatummy F8" could return
# other nearby places alongside (or instead of) the real business, and the
# actor crawled all of them with zero validation. 52+ completely unrelated
# businesses (a hotel, a Thai restaurant, several biryani spots) ended up
# stored as Anatummy's own reviews. This filter is the second line of
# defense (maxCrawledPlacesPerSearch=1 is the first) -- discard anything
# whose title doesn't actually contain the store's own name.

class TestGoogleMapsNameFilter:
    def _mock_run(self, monkeypatch, items):
        from app.review_sources import google_maps as gm

        mock_client = MagicMock()
        mock_client.actor.return_value.call.return_value = {"defaultDatasetId": "ds1"}
        mock_client.dataset.return_value.iterate_items.return_value = iter(items)
        monkeypatch.setattr(gm, "ApifyClient", lambda api_key: mock_client)
        return mock_client

    def test_discards_places_not_matching_business_name(self, monkeypatch):
        from app.review_sources.google_maps import fetch_reviews
        self._mock_run(monkeypatch, [
            {"title": "Anatummy F8", "reviews": [{"text": "Great burgers", "stars": 5}]},
            {"title": "Hotel One Super, Islamabad", "reviews": [{"text": "Nice stay", "stars": 5}]},
            {"title": "Tiger Temple", "reviews": [{"text": "Best thai food", "stars": 5}]},
        ])
        results = fetch_reviews("fake-key", ["Anatummy"], "Islamabad", business_name="Anatummy")
        assert len(results) == 1
        assert results[0]["source"] == "Google Maps - Anatummy F8"

    def test_case_insensitive_match(self, monkeypatch):
        from app.review_sources.google_maps import fetch_reviews
        self._mock_run(monkeypatch, [
            {"title": "ANATUMMY Beverly Centre", "reviews": [{"text": "Loved it", "stars": 5}]},
        ])
        results = fetch_reviews("fake-key", ["Anatummy"], "Islamabad", business_name="Anatummy")
        assert len(results) == 1

    def test_no_business_name_skips_filtering(self, monkeypatch):
        """Backward compatible -- callers that don't pass business_name get
        the old unfiltered behavior rather than everything being discarded."""
        from app.review_sources.google_maps import fetch_reviews
        self._mock_run(monkeypatch, [
            {"title": "Some Place", "reviews": [{"text": "hello", "stars": 5}]},
        ])
        results = fetch_reviews("fake-key", ["Some Place"], "Islamabad")
        assert len(results) == 1

    def test_max_crawled_places_per_search_is_one(self, monkeypatch):
        """The primary defense -- only the single best match per search
        term should ever be requested from the actor."""
        from app.review_sources.google_maps import fetch_reviews
        mock_client = self._mock_run(monkeypatch, [])
        fetch_reviews("fake-key", ["Anatummy"], "Islamabad", business_name="Anatummy")
        call_kwargs = mock_client.actor.return_value.call.call_args
        run_input = call_kwargs.kwargs.get("run_input") or call_kwargs.args[0]
        assert run_input["maxCrawledPlacesPerSearch"] == 1
