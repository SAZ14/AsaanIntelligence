"""Review dedup integrity and the "show me positive/negative/ignored/posted
reviews" natural-language feature.

Three real problems fixed together:
1. save_review_finding() used to overwrite an existing review's ai_summary
   on every re-scrape -- including its workflow status. A review already
   marked "posted" or "ignored" by staff could silently reset back to
   "pending"/"auto_closed" if the same review was scraped again later.
2. The check was hard-capped to the newest 50 reviews per run (previously
   confirmed live: a real scrape found 300+). Removed in favor of
   processing everything scraped, made safe and cheap by (1): a review
   already seen is skipped entirely before any LLM call, so steady-state
   cost per check is just the delta of genuinely new reviews.
3. Natural-language "show me positive reviews" had no way to answer
   accurately -- the chat function only ever saw 10 recent reviews with no
   sentiment/status filtering at all. Added a dedicated classify-then-
   deterministically-query path instead of asking an LLM to filter/count
   across a large embedded list (unreliable for exact counts).
"""
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import seed_chain, seed_store, TestSession


@pytest.fixture
def store_id():
    chain_id = seed_chain("Review Dedup Chain")
    return seed_store(chain_id, name="Review Dedup Cafe", location="F-7, Islamabad")


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """Pagination state lives in Redis (app/core/cache.py) -- without a
    real or fake connection, cache.get/set are no-ops and NEXT would always
    report "nothing to continue" regardless of whether a list request was
    just made. Same fixture pattern as test_guards.py/test_freshness_cache.py."""
    fakeredis = pytest.importorskip("fakeredis")
    import app.core.cache as cache
    monkeypatch.setattr(cache, "_client", fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(cache, "_unavailable", False)


def _seed_run(store_id):
    from app.core.db import ScoutRun
    with TestSession() as db:
        run = ScoutRun(store_id=store_id, command="whatsapp_check", status="ok")
        db.add(run)
        db.commit()
        db.refresh(run)
        return run.id


def _raw_review(hash_: str, text: str = "some review", rating=None, source="Google Maps - Cafe") -> dict:
    return {"source": source, "text": text, "rating": rating, "hash": hash_, "url": "", "review_date": ""}


# ── existing_content_hashes / save_review_finding dedup ─────────────────────

class TestDedupIntegrity:
    def test_new_review_is_saved_as_new(self, store_id):
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        result = review_db.save_review_finding(
            store_id, run_id, "Cafe", _raw_review("h1"), {"status": "pending"},
        )
        assert result == "new"

    def test_duplicate_hash_is_skipped_not_overwritten(self, store_id):
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        review_db.save_review_finding(
            store_id, run_id, "Cafe", _raw_review("h1"), {"status": "posted", "draft_reply": "Thanks!"},
        )
        # Same content_hash comes back in a later scrape, freshly
        # "classified" as if it were new again.
        result = review_db.save_review_finding(
            store_id, run_id, "Cafe", _raw_review("h1"), {"status": "pending", "draft_reply": ""},
        )
        assert result == "duplicate"

        # The ORIGINAL status/draft must survive untouched.
        matches, total = review_db.list_reviews(store_id, status="posted")
        assert total == 1
        assert matches[0]["ai_summary"]["draft_reply"] == "Thanks!"

    def test_existing_content_hashes_reports_only_stored_ones(self, store_id):
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        review_db.save_review_finding(store_id, run_id, "Cafe", _raw_review("h1"), {"status": "pending"})
        review_db.save_review_finding(store_id, run_id, "Cafe", _raw_review("h2"), {"status": "pending"})

        found = review_db.existing_content_hashes(store_id, ["h1", "h2", "h3"])
        assert found == {"h1", "h2"}

    def test_existing_content_hashes_empty_input(self, store_id):
        from app.review_sources import db as review_db
        assert review_db.existing_content_hashes(store_id, []) == set()

    def test_ignored_review_status_survives_a_repeat_scrape(self, store_id):
        """Direct reproduction of the real bug: a review gets ignored by
        staff, then the exact same review is scraped again (e.g. Google
        Maps serves it again in a later run) -- its status must not reset."""
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        review_db.save_review_finding(store_id, run_id, "Cafe", _raw_review("h1"), {"status": "pending"})
        pending = review_db.get_pending_finding(store_id)
        review_db.update_finding_summary(pending["id"], {"status": "ignored"})

        # Re-scraped in a later run with a freshly-computed (but identical
        # hash) classification result.
        review_db.save_review_finding(
            store_id, run_id, "Cafe", _raw_review("h1"), {"status": "pending"},
        )

        assert review_db.get_pending_finding(store_id) is None  # still ignored, not pending again
        matches, _ = review_db.list_reviews(store_id, status="ignored")
        assert len(matches) == 1


# ── _check_reviews: dedup happens before any LLM call ────────────────────────

class TestCheckReviewsDedup:
    def test_already_seen_reviews_are_never_reclassified(self, store_id):
        """The real point of dedup-before-classify: an already-stored
        review must not cost a second classify_reviews_batch/draft_replies
        pass just because it showed up in the scrape again."""
        from app.agents.reputation import _check_reviews
        from app.review_sources import db as review_db

        run_id = _seed_run(store_id)
        review_db.save_review_finding(
            store_id, run_id, "Review Dedup Cafe", _raw_review("h1", rating=5.0), {"status": "auto_closed"},
        )

        with patch("app.review_sources.pipeline.run_pipeline",
                    return_value=([_raw_review("h1", rating=5.0)], ["google_maps"], [])), \
             patch("app.agents.reputation.classify_reviews_batch") as mock_classify, \
             patch("app.core.llm.get_client", return_value=MagicMock()):
            reply = _check_reviews(store_id, "Review Dedup Cafe")

        mock_classify.assert_not_called()
        assert "no new reviews" in reply.lower() or "up to date" in reply.lower()

    def test_genuinely_new_reviews_still_get_classified(self, store_id):
        from app.agents.reputation import _check_reviews

        with patch("app.review_sources.pipeline.run_pipeline",
                    return_value=([_raw_review("new1", rating=5.0)], ["google_maps"], [])), \
             patch("app.agents.reputation.classify_reviews_batch") as mock_classify, \
             patch("app.agents.reputation.draft_replies", side_effect=lambda revs, *a, **kw: revs), \
             patch("app.core.llm.get_client", return_value=MagicMock()):
            _check_reviews(store_id, "Review Dedup Cafe")

        mock_classify.assert_called_once()

    def test_no_hard_cap_at_fifty_reviews(self, store_id):
        """Confirmed live: a real scrape found 300+ reviews. All of them
        must reach classification, not just the newest 50."""
        from app.agents.reputation import _check_reviews, MAX_NEW_REVIEWS_PER_CHECK

        assert MAX_NEW_REVIEWS_PER_CHECK > 50

        many_reviews = [_raw_review(f"new{i}", rating=4.0) for i in range(120)]
        with patch("app.review_sources.pipeline.run_pipeline",
                    return_value=(many_reviews, ["google_maps"], [])), \
             patch("app.agents.reputation.classify_reviews_batch") as mock_classify, \
             patch("app.agents.reputation.draft_replies", side_effect=lambda revs, *a, **kw: revs), \
             patch("app.core.llm.get_client", return_value=MagicMock()):
            _check_reviews(store_id, "Review Dedup Cafe")

        classified = mock_classify.call_args.args[0]
        assert len(classified) == 120


# ── Non-review content filtering ─────────────────────────────────────────────
# Confirmed live: Instagram comments (casual social replies, not a dedicated
# review system) regularly mix genuine feedback with questions ("do you
# deliver?"), well-wishes ("good luck!"), business inquiries
# ("collaboration"), and off-topic remarks -- all of which were getting a
# sentiment tag and saved as if they were real reviews. classify_reviews_
# batch now also flags is_review; non-review content gets dropped before
# drafting a reply or saving, not just before someone reads it.

class TestNonReviewContentFiltering:
    def test_non_review_items_are_not_saved(self, store_id):
        def fake_classify(reviews, client):
            for ra in reviews:
                if "good luck" in ra.text.lower():
                    ra.is_review = False
            return reviews

        with patch("app.review_sources.pipeline.run_pipeline",
                    return_value=([
                        _raw_review("real1", rating=5.0, text="Great burgers, loved it!"),
                        _raw_review("wellwish1", rating=None, text="Good luck Anatummy!"),
                    ], ["google_maps"], [])), \
             patch("app.agents.reputation.classify_reviews_batch", side_effect=fake_classify), \
             patch("app.agents.reputation.draft_replies", side_effect=lambda revs, *a, **kw: revs), \
             patch("app.core.llm.get_client", return_value=MagicMock()):
            from app.agents.reputation import _check_reviews
            _check_reviews(store_id, "Review Dedup Cafe")

        from app.review_sources import db as review_db
        matches, total = review_db.list_reviews(store_id)
        assert total == 1
        assert matches[0]["content_hash"] == "real1"

    def test_all_non_review_reports_zero_new(self, store_id):
        def fake_classify(reviews, client):
            for ra in reviews:
                ra.is_review = False
            return reviews

        with patch("app.review_sources.pipeline.run_pipeline",
                    return_value=([_raw_review("q1", rating=None, text="do you deliver??")], ["google_maps"], [])), \
             patch("app.agents.reputation.classify_reviews_batch", side_effect=fake_classify), \
             patch("app.agents.reputation.draft_replies", side_effect=lambda revs, *a, **kw: revs), \
             patch("app.core.llm.get_client", return_value=MagicMock()):
            from app.agents.reputation import _check_reviews
            reply = _check_reviews(store_id, "Review Dedup Cafe")

        assert "no new reviews" in reply.lower() or "up to date" in reply.lower()


class TestClassifyReviewsBatchIsReviewParsing:
    """classify_reviews_batch's LLM output parsing for the new is_review
    field -- structural correctness with a mocked response, matching the
    pattern used for other classifier tests in this suite. Real-model
    accuracy verified separately, live."""

    def _mock_client(self, reply: str):
        client = MagicMock()
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = reply
        client.chat.completions.create.return_value = resp
        return client

    def test_parses_is_review_yes(self):
        from app.agents.reputation import classify_reviews_batch, ReviewAnalysis
        ra = ReviewAnalysis(review_id="1", source="ig", rating=None, posted_at="2026-07-01",
                            reviewer_name="x", text="so good")
        with patch("app.core.llm.get_model", return_value="glm-4.7"), \
             patch("app.core.llm.nothink_kwargs", return_value={}):
            classify_reviews_batch([ra], self._mock_client("1. praise,positive,yes"))
        assert ra.is_review is True

    def test_parses_is_review_no(self):
        from app.agents.reputation import classify_reviews_batch, ReviewAnalysis
        ra = ReviewAnalysis(review_id="1", source="ig", rating=None, posted_at="2026-07-01",
                            reviewer_name="x", text="do you deliver?")
        with patch("app.core.llm.get_model", return_value="glm-4.7"), \
             patch("app.core.llm.nothink_kwargs", return_value={}):
            classify_reviews_batch([ra], self._mock_client("1. other,neutral,no"))
        assert ra.is_review is False

    def test_malformed_response_keeps_default_true(self):
        """Untrusted output -- anything that doesn't parse cleanly must
        not silently drop a review that should have been kept."""
        from app.agents.reputation import classify_reviews_batch, ReviewAnalysis
        ra = ReviewAnalysis(review_id="1", source="ig", rating=None, posted_at="2026-07-01",
                            reviewer_name="x", text="great food")
        with patch("app.core.llm.get_model", return_value="glm-4.7"), \
             patch("app.core.llm.nothink_kwargs", return_value={}):
            classify_reviews_batch([ra], self._mock_client("garbage output"))
        assert ra.is_review is True

    def test_literal_N_placeholder_still_parses_positionally(self):
        """Confirmed live: the model doesn't reliably substitute the "N."
        line-number placeholder with the real index -- sometimes every
        line comes back as literal "N." instead of "1.", "2.", etc. The
        old index-trusting parser silently matched zero lines in this
        case, leaving every review at its default classification with no
        error. Must still work by matching lines positionally."""
        from app.agents.reputation import classify_reviews_batch, ReviewAnalysis
        ras = [
            ReviewAnalysis(review_id="1", source="ig", rating=None, posted_at="2026-07-01",
                            reviewer_name="x", text="good luck!"),
            ReviewAnalysis(review_id="2", source="ig", rating=None, posted_at="2026-07-01",
                            reviewer_name="x", text="best burgers ever"),
        ]
        reply = "N. other,neutral,no\nN. praise,positive,yes"
        with patch("app.core.llm.get_model", return_value="glm-4.7"), \
             patch("app.core.llm.nothink_kwargs", return_value={}):
            classify_reviews_batch(ras, self._mock_client(reply))
        assert ras[0].is_review is False
        assert ras[1].is_review is True
        assert ras[1].sentiment == "positive"

    def test_echoed_instruction_line_does_not_shift_results(self):
        """Confirmed live: the model can echo part of the instruction
        template as a stray first line ("N. issue_class,sentiment,
        is_review") before the real classifications. That line
        superficially matches the same regex shape -- must be rejected
        by value validation (not a real issue_class/sentiment/yes-no),
        not accidentally consumed as review #1's result and shifting
        every subsequent review off by one."""
        from app.agents.reputation import classify_reviews_batch, ReviewAnalysis
        ras = [
            ReviewAnalysis(review_id="1", source="ig", rating=None, posted_at="2026-07-01",
                            reviewer_name="x", text="good luck!"),
            ReviewAnalysis(review_id="2", source="ig", rating=None, posted_at="2026-07-01",
                            reviewer_name="x", text="best burgers ever"),
        ]
        reply = "N. issue_class,sentiment,is_review\n1. other,neutral,no\n2. praise,positive,yes"
        with patch("app.core.llm.get_model", return_value="glm-4.7"), \
             patch("app.core.llm.nothink_kwargs", return_value={}):
            classify_reviews_batch(ras, self._mock_client(reply))
        assert ras[0].is_review is False
        assert ras[1].is_review is True
        assert ras[1].sentiment == "positive"

    def test_zero_matches_triggers_one_retry_that_can_succeed(self):
        """Confirmed live: a whole batch can come back with zero valid
        classification lines (a run of "N."s with no real values). A
        second attempt at the same non-deterministic call has reliably
        produced well-formed output when this was tested live -- must
        actually retry rather than silently accepting the failure."""
        from app.agents.reputation import classify_reviews_batch, ReviewAnalysis
        ra = ReviewAnalysis(review_id="1", source="ig", rating=None, posted_at="2026-07-01",
                            reviewer_name="x", text="best burgers ever")
        client = MagicMock()
        bad_resp = MagicMock()
        bad_resp.choices = [MagicMock()]
        bad_resp.choices[0].message.content = "garbage, no valid lines at all"
        good_resp = MagicMock()
        good_resp.choices = [MagicMock()]
        good_resp.choices[0].message.content = "1. praise,positive,yes"
        client.chat.completions.create.side_effect = [bad_resp, good_resp]

        with patch("app.core.llm.get_model", return_value="glm-4.7"), \
             patch("app.core.llm.nothink_kwargs", return_value={}):
            classify_reviews_batch([ra], client)

        assert client.chat.completions.create.call_count == 2
        assert ra.is_review is True
        assert ra.sentiment == "positive"
        assert ra.issue_class == "praise"

    def test_second_attempt_also_failing_does_not_retry_forever(self):
        from app.agents.reputation import classify_reviews_batch, ReviewAnalysis
        ra = ReviewAnalysis(review_id="1", source="ig", rating=None, posted_at="2026-07-01",
                            reviewer_name="x", text="best burgers ever")
        client = MagicMock()
        client.chat.completions.create.return_value.choices = [MagicMock()]
        client.chat.completions.create.return_value.choices[0].message.content = "garbage output"

        with patch("app.core.llm.get_model", return_value="glm-4.7"), \
             patch("app.core.llm.nothink_kwargs", return_value={}):
            classify_reviews_batch([ra], client)

        assert client.chat.completions.create.call_count == 2  # exactly one retry, not unbounded
        assert ra.is_review is True  # safe default preserved

    def test_rule_based_historical_path_defaults_to_review(self):
        """Older RATED reviews go through the rule-based path (no LLM
        call) -- a star rating means it already went through the
        platform's own review-submission flow, so it's trusted as real."""
        from app.agents.reputation import classify_reviews_batch, ReviewAnalysis
        from datetime import datetime, timedelta
        old_date = (datetime.utcnow() - timedelta(days=400)).strftime("%Y-%m-%d")
        ra = ReviewAnalysis(review_id="1", source="google_maps", rating=5,
                            posted_at=old_date, reviewer_name="x", text="great")
        classify_reviews_batch([ra], MagicMock())
        assert ra.is_review is True


# ── _check_reviews in-flight guard: atomic Redis lock, no race ──────────────
# Mirrors the same fix applied to scout (app/agents/scout/pipeline.py's
# run()): the old guard was a Postgres SELECT-then-INSERT (check for a
# "running" row, then later write one), which is racy -- two callers
# (the cron poll and a staff message arriving moments apart) could both
# see "not running yet" and both start a scrape. Replaced with an atomic
# Redis lock (SET NX -- cache.try_lock), so at most one scrape ever
# actually runs for a store at a time, regardless of who triggered it.

class TestCheckReviewsInFlightLock:
    def test_recognizes_a_lock_held_by_someone_else_as_in_flight(self, store_id):
        from app.core import cache as _cache
        from app.agents.reputation import _check_reviews, _reputation_live_lock_key, REPUTATION_RUN_LOCK_MINUTES

        assert _cache.try_lock(_reputation_live_lock_key(store_id), ttl_seconds=REPUTATION_RUN_LOCK_MINUTES * 60)

        with patch("app.review_sources.pipeline.run_pipeline") as mock_pipeline:
            reply = _check_reviews(store_id, "Review Dedup Cafe")

        mock_pipeline.assert_not_called()
        assert "already in progress" in reply.lower()

    def test_proceeds_and_releases_the_lock_once_the_scrape_completes(self, store_id):
        from app.core import cache as _cache
        from app.agents.reputation import _check_reviews, _reputation_live_lock_key

        with patch("app.review_sources.pipeline.run_pipeline",
                    return_value=([_raw_review("new1", rating=5.0)], ["google_maps"], [])), \
             patch("app.agents.reputation.classify_reviews_batch"), \
             patch("app.agents.reputation.draft_replies", side_effect=lambda revs, *a, **kw: revs), \
             patch("app.core.llm.get_client", return_value=MagicMock()):
            _check_reviews(store_id, "Review Dedup Cafe")

        # Lock must be free again after a completed run, not held for its
        # full TTL -- otherwise every store would wait out the TTL between
        # any two checks even when nothing is actually still running.
        assert _cache.try_lock(_reputation_live_lock_key(store_id), ttl_seconds=60) is True

    def test_releases_the_lock_even_if_the_scrape_raises(self, store_id):
        """try/finally must release the lock on the error path too, or a
        single failed Apify call would strand every future check behind
        the lock's full TTL."""
        from app.core import cache as _cache
        from app.agents.reputation import _check_reviews, _reputation_live_lock_key

        with patch("app.review_sources.pipeline.run_pipeline", side_effect=RuntimeError("apify down")):
            reply = _check_reviews(store_id, "Review Dedup Cafe")

        assert "failed" in reply.lower()
        assert _cache.try_lock(_reputation_live_lock_key(store_id), ttl_seconds=60) is True

    def test_only_one_of_two_simultaneous_callers_acquires_the_lock(self, store_id):
        from app.core import cache as _cache
        from app.agents.reputation import _reputation_live_lock_key

        key = _reputation_live_lock_key(store_id)
        assert _cache.try_lock(key, ttl_seconds=60) is True
        assert _cache.try_lock(key, ttl_seconds=60) is False


# ── list_reviews ──────────────────────────────────────────────────────────────

class TestListReviews:
    def _seed(self, store_id, hash_, sentiment=None, status="pending", rating=None):
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        summary = {"status": status}
        if sentiment:
            summary["sentiment"] = sentiment
        review_db.save_review_finding(store_id, run_id, "Cafe", _raw_review(hash_, rating=rating), summary)

    def test_filters_by_sentiment(self, store_id):
        from app.review_sources import db as review_db
        self._seed(store_id, "p1", sentiment="positive")
        self._seed(store_id, "n1", sentiment="negative")
        matches, total = review_db.list_reviews(store_id, sentiment="positive")
        assert total == 1
        assert matches[0]["content_hash"] == "p1"

    def test_filters_by_status(self, store_id):
        from app.review_sources import db as review_db
        self._seed(store_id, "i1", status="ignored")
        self._seed(store_id, "p1", status="pending")
        matches, total = review_db.list_reviews(store_id, status="ignored")
        assert total == 1
        assert matches[0]["content_hash"] == "i1"

    def test_no_filter_returns_everything(self, store_id):
        from app.review_sources import db as review_db
        self._seed(store_id, "a1")
        self._seed(store_id, "a2")
        matches, total = review_db.list_reviews(store_id)
        assert total == 2

    def test_total_count_exceeds_display_limit_when_capped(self, store_id):
        from app.review_sources import db as review_db
        for i in range(20):
            self._seed(store_id, f"h{i}", sentiment="positive")
        matches, total = review_db.list_reviews(store_id, sentiment="positive", limit=15)
        assert len(matches) == 15
        assert total == 20

    def test_sorted_by_post_date_not_insertion_order(self, store_id):
        """The real bug: previously sorted by DB id (scrape/insertion
        order), not the review's actual post date -- an old review
        scraped in a later run would jump to the front. Deliberately
        insert out of chronological order to prove the fix."""
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        reviews = [
            ("oldest", "2025-01-01"),
            ("newest", "2026-06-01"),
            ("middle", "2025-06-15"),
        ]
        # Inserted oldest-id-first in a DELIBERATELY non-chronological
        # order relative to post_date, so id order and date order disagree.
        for hash_, date in reviews:
            review_db.save_review_finding(
                store_id, run_id, "Cafe",
                {"source": "Google Maps", "text": f"review {hash_}", "rating": 5.0,
                 "hash": hash_, "url": "", "review_date": date},
                {"status": "auto_closed"},
            )
        matches, total = review_db.list_reviews(store_id)
        assert total == 3
        assert [m["content_hash"] for m in matches] == ["newest", "middle", "oldest"]

    def test_reviews_with_no_post_date_sort_last(self, store_id):
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        review_db.save_review_finding(
            store_id, run_id, "Cafe",
            {"source": "Google Maps", "text": "has a date", "rating": 5.0,
             "hash": "dated", "url": "", "review_date": "2026-01-01"},
            {"status": "auto_closed"},
        )
        review_db.save_review_finding(
            store_id, run_id, "Cafe",
            {"source": "Google Maps", "text": "no date", "rating": 5.0,
             "hash": "undated", "url": "", "review_date": ""},
            {"status": "auto_closed"},
        )
        matches, total = review_db.list_reviews(store_id)
        assert [m["content_hash"] for m in matches] == ["dated", "undated"]

    def test_days_back_filters_out_older_reviews(self, store_id):
        from app.review_sources import db as review_db
        from datetime import datetime, timedelta
        run_id = _seed_run(store_id)
        today = datetime.utcnow().strftime("%Y-%m-%d")
        two_weeks_ago = (datetime.utcnow() - timedelta(days=14)).strftime("%Y-%m-%d")
        review_db.save_review_finding(
            store_id, run_id, "Cafe",
            {"source": "Google Maps", "text": "recent", "rating": 5.0,
             "hash": "recent1", "url": "", "review_date": today},
            {"status": "auto_closed"},
        )
        review_db.save_review_finding(
            store_id, run_id, "Cafe",
            {"source": "Google Maps", "text": "old", "rating": 5.0,
             "hash": "old1", "url": "", "review_date": two_weeks_ago},
            {"status": "auto_closed"},
        )
        matches, total = review_db.list_reviews(store_id, days_back=7)
        assert total == 1
        assert matches[0]["content_hash"] == "recent1"

    def test_days_back_excludes_reviews_with_no_post_date(self, store_id):
        """No date to compare against a window means it can't honestly
        be said to fall within it either way -- excluded, not assumed."""
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        review_db.save_review_finding(
            store_id, run_id, "Cafe", _raw_review("nodate1", rating=5.0, text="undated"),
            {"status": "auto_closed"},
        )
        matches, total = review_db.list_reviews(store_id, days_back=7)
        assert total == 0

    def test_no_days_back_includes_everything_regardless_of_age(self, store_id):
        from app.review_sources import db as review_db
        from datetime import datetime, timedelta
        run_id = _seed_run(store_id)
        very_old = (datetime.utcnow() - timedelta(days=400)).strftime("%Y-%m-%d")
        review_db.save_review_finding(
            store_id, run_id, "Cafe",
            {"source": "Google Maps", "text": "ancient", "rating": 5.0,
             "hash": "ancient1", "url": "", "review_date": very_old},
            {"status": "auto_closed"},
        )
        matches, total = review_db.list_reviews(store_id, days_back=None)
        assert total == 1

    def test_days_back_is_midnight_aligned_not_a_precise_24h_multiple(self):
        """Confirmed live: post_date is always stored at midnight (see
        save_review_finding), so a cutoff of "now minus N days" is a
        precise timestamp including the current time of day -- a review
        posted "yesterday" (midnight) fell BEFORE "now minus 1 day" any
        time after midnight today, since yesterday's midnight is earlier
        in the day than right now. days_back=2 must include a review
        from exactly yesterday's calendar date regardless of what time
        of day "now" is when the query runs."""
        from app.review_sources import db as review_db
        from datetime import datetime, timedelta
        chain_id = seed_chain("Midnight Align Chain")
        store_id = seed_store(chain_id, name="Midnight Align Cafe")
        run_id = _seed_run(store_id)
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        review_db.save_review_finding(
            store_id, run_id, "Cafe",
            {"source": "Google Maps", "text": "posted yesterday", "rating": 5.0,
             "hash": "yesterday1", "url": "", "review_date": yesterday},
            {"status": "auto_closed"},
        )
        # days_back=2 is meant to mean "today + yesterday" -- must find it
        # regardless of what time of day this test happens to run at.
        matches, total = review_db.list_reviews(store_id, days_back=2)
        assert total == 1


# ── _classify_review_query ────────────────────────────────────────────────────

class TestClassifyReviewQuery:
    def _mock_client(self, reply: str):
        client = MagicMock()
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = reply
        client.chat.completions.create.return_value = resp
        return client

    def test_positive_request_detected(self):
        from app.agents.reputation import _classify_review_query
        with patch("app.core.llm.get_client", return_value=self._mock_client("positive,none")):
            sentiment, status = _classify_review_query("show me positive reviews")
        assert sentiment == "positive"
        assert status is None

    def test_status_request_detected(self):
        from app.agents.reputation import _classify_review_query
        with patch("app.core.llm.get_client", return_value=self._mock_client("none,ignored")):
            sentiment, status = _classify_review_query("what did we ignore")
        assert sentiment is None
        assert status == "ignored"

    def test_non_list_question_returns_none_none(self):
        from app.agents.reputation import _classify_review_query
        with patch("app.core.llm.get_client", return_value=self._mock_client("none,none")):
            sentiment, status = _classify_review_query("how is our rating trending")
        assert sentiment is None
        assert status is None

    def test_falls_back_to_keywords_on_llm_failure(self):
        from app.agents.reputation import _classify_review_query
        with patch("app.core.llm.get_client", side_effect=RuntimeError("down")):
            sentiment, status = _classify_review_query("show me negative reviews")
        assert sentiment == "negative"


# ── _excerpt: truncate but say so ────────────────────────────────────────────
# Confirmed live: the old hard [:120]/[:150] cut with no marker at all made
# a long review just stop mid-sentence -- "...were " with nothing after it
# -- which read as missing/corrupted data even though the full text was
# always intact in the DB (confirmed: not an Apify scraping gap, purely a
# display issue). 45 of 198 real Anatummy reviews exceeded the old cutoff.

class TestExcerpt:
    def test_short_text_returned_unchanged(self):
        from app.agents.reputation import _excerpt
        assert _excerpt("short text", 120) == "short text"

    def test_long_text_gets_ellipsis_marker(self):
        from app.agents.reputation import _excerpt
        long_text = "a" * 300
        result = _excerpt(long_text, 120)
        assert result.endswith("...")
        assert len(result) == 123  # 120 chars + "..."

    def test_exactly_at_limit_not_truncated(self):
        from app.agents.reputation import _excerpt
        text = "a" * 120
        assert _excerpt(text, 120) == text

    def test_empty_text_returns_empty(self):
        from app.agents.reputation import _excerpt
        assert _excerpt("", 120) == ""
        assert _excerpt(None, 120) == ""


# ── _list_reviews_page (formatting + pagination) ─────────────────────────────

class TestListReviewsPage:
    def test_formats_matches_with_count(self, store_id):
        from app.agents.reputation import _list_reviews_page
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        review_db.save_review_finding(
            store_id, run_id, "Cafe", _raw_review("p1", rating=5.0, text="Loved it!"),
            {"status": "auto_closed", "sentiment": "positive"},
        )
        reply = _list_reviews_page(store_id, "Review Dedup Cafe", "+923001234567", "positive", None)
        assert "of 1" in reply
        assert "Loved it!" in reply

    def test_each_line_shows_the_review_date(self, store_id):
        from app.agents.reputation import _list_reviews_page
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        review_db.save_review_finding(
            store_id, run_id, "Cafe",
            {"source": "Google Maps", "text": "Great food", "rating": 5.0,
             "hash": "dated1", "url": "", "review_date": "2026-05-15"},
            {"status": "auto_closed", "sentiment": "positive"},
        )
        reply = _list_reviews_page(store_id, "Cafe", "+923001234567", "positive", None)
        assert "2026-05-15" in reply

    def test_missing_date_shows_placeholder_not_error(self, store_id):
        from app.agents.reputation import _list_reviews_page
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        review_db.save_review_finding(
            store_id, run_id, "Cafe", _raw_review("nodate1", rating=5.0, text="Good"),
            {"status": "auto_closed", "sentiment": "positive"},
        )
        reply = _list_reviews_page(store_id, "Cafe", "+923001234567", "positive", None)
        assert "date unknown" in reply

    def test_instagram_reviews_dont_show_no_rating(self, store_id):
        """Instagram content (posts/comments) never has a star rating --
        that's not a data gap worth flagging on every single line the
        way a genuinely missing Google Maps rating would be."""
        from app.agents.reputation import _list_reviews_page
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        review_db.save_review_finding(
            store_id, run_id, "Cafe",
            {"source": "Instagram - anatummyisb comment", "text": "so good", "rating": None,
             "hash": "ig1", "url": "", "review_date": "2026-07-01"},
            {"status": "auto_closed", "sentiment": "positive"},
        )
        reply = _list_reviews_page(store_id, "Cafe", "+923001234567", "positive", None)
        assert "no rating" not in reply.lower()
        assert "[Instagram, 2026-07-01]" in reply

    def test_google_maps_review_still_shows_no_rating_when_missing(self, store_id):
        """Unlike Instagram, a Google Maps review missing its rating IS
        a real gap -- still worth showing."""
        from app.agents.reputation import _list_reviews_page
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        review_db.save_review_finding(
            store_id, run_id, "Cafe",
            {"source": "Google Maps - Anatummy", "text": "decent", "rating": None,
             "hash": "gm1", "url": "", "review_date": "2026-07-01"},
            {"status": "auto_closed", "sentiment": "positive"},
        )
        reply = _list_reviews_page(store_id, "Cafe", "+923001234567", "positive", None)
        assert "no rating" in reply.lower()

    def test_no_matches_says_so_clearly(self, store_id):
        from app.agents.reputation import _list_reviews_page
        reply = _list_reviews_page(store_id, "Review Dedup Cafe", "+923001234567", "negative", None)
        assert "no" in reply.lower()
        assert "negative" in reply.lower()

    def test_more_than_one_page_offers_next(self, store_id):
        from app.agents.reputation import _list_reviews_page
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        for i in range(15):
            review_db.save_review_finding(
                store_id, run_id, "Cafe", _raw_review(f"p{i}", rating=5.0),
                {"status": "auto_closed", "sentiment": "positive"},
            )
        reply = _list_reviews_page(store_id, "Review Dedup Cafe", "+923001234567", "positive", None)
        assert "1-10 of 15" in reply
        assert "NEXT" in reply
        assert "5 remaining" in reply

    def test_exactly_one_page_does_not_offer_next(self, store_id):
        from app.agents.reputation import _list_reviews_page
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        for i in range(10):
            review_db.save_review_finding(
                store_id, run_id, "Cafe", _raw_review(f"p{i}", rating=5.0),
                {"status": "auto_closed", "sentiment": "positive"},
            )
        reply = _list_reviews_page(store_id, "Review Dedup Cafe", "+923001234567", "positive", None)
        assert "1-10 of 10" in reply
        assert "NEXT" not in reply

    def test_next_command_advances_to_the_second_page(self, store_id):
        from app.agents.reputation import process_reputation_owner_reply
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        for i in range(15):
            review_db.save_review_finding(
                store_id, run_id, "Cafe", _raw_review(f"p{i}", rating=5.0, text=f"review number {i}"),
                {"status": "auto_closed", "sentiment": "positive"},
            )
        process_reputation_owner_reply("+923001234567", "positive reviews", store_id=store_id)
        second_page = process_reputation_owner_reply("+923001234567", "next", store_id=store_id)
        assert "11-15 of 15" in second_page
        assert "NEXT" not in second_page  # exhausted, no more pages

    def test_next_without_prior_list_request_says_so(self, store_id):
        from app.agents.reputation import process_reputation_owner_reply
        reply = process_reputation_owner_reply("+923001234567", "next", store_id=store_id)
        assert "nothing to continue" in reply.lower()

    def test_degrades_gracefully_without_redis(self, store_id, monkeypatch):
        """Fails open the same way every other Redis-backed guard in this
        codebase does: NEXT just says there's nothing to continue rather
        than erroring, instead of silently pretending to paginate with no
        real state behind it."""
        import app.core.cache as cache
        monkeypatch.setattr(cache, "_client", None)
        monkeypatch.setattr(cache, "_unavailable", True)

        from app.agents.reputation import process_reputation_owner_reply
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        for i in range(15):
            review_db.save_review_finding(
                store_id, run_id, "Cafe", _raw_review(f"p{i}", rating=5.0),
                {"status": "auto_closed", "sentiment": "positive"},
            )
        first_page = process_reputation_owner_reply("+923001234567", "positive reviews", store_id=store_id)
        assert "1-10 of 15" in first_page  # the page itself still works, just no NEXT
        assert "NEXT" not in first_page

        reply = process_reputation_owner_reply("+923001234567", "next", store_id=store_id)
        assert "nothing to continue" in reply.lower()

    def test_pagination_state_is_scoped_per_phone_number(self, store_id):
        """Two staff members paging through different filters at once must
        not collide."""
        from app.agents.reputation import process_reputation_owner_reply
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        for i in range(15):
            review_db.save_review_finding(
                store_id, run_id, "Cafe", _raw_review(f"p{i}", rating=5.0),
                {"status": "auto_closed", "sentiment": "positive"},
            )
            review_db.save_review_finding(
                store_id, run_id, "Cafe", _raw_review(f"n{i}", rating=1.0),
                {"status": "auto_closed", "sentiment": "negative"},
            )
        process_reputation_owner_reply("+923001111111", "positive reviews", store_id=store_id)
        process_reputation_owner_reply("+923002222222", "negative reviews", store_id=store_id)

        page2_a = process_reputation_owner_reply("+923001111111", "next", store_id=store_id)
        page2_b = process_reputation_owner_reply("+923002222222", "next", store_id=store_id)
        assert "positive" in page2_a.lower()
        assert "negative" in page2_b.lower()


# ── Routing: list requests bypass the general chat entirely ─────────────────

class TestReviewListRouting:
    def test_list_request_never_reaches_general_chat(self, store_id):
        from app.agents.reputation import process_reputation_owner_reply
        with patch("app.agents.reputation._classify_review_query", return_value=("positive", None)), \
             patch("app.agents.reputation._list_reviews_page", return_value="listed") as mock_list, \
             patch("app.agents.reputation._chat_about_reviews") as mock_chat:
            reply = process_reputation_owner_reply("+923001234567", "show me positive reviews perhaps", store_id=store_id)
        mock_list.assert_called_once()
        mock_chat.assert_not_called()
        assert reply == "listed"

    def test_exact_trigger_phrase_bypasses_classifier_entirely(self, store_id):
        from app.agents.reputation import process_reputation_owner_reply
        with patch("app.agents.reputation._classify_review_query") as mock_classify, \
             patch("app.agents.reputation._list_reviews_page", return_value="listed") as mock_list:
            process_reputation_owner_reply("+923001234567", "positive reviews", store_id=store_id)
        mock_classify.assert_not_called()
        mock_list.assert_called_once_with(store_id, "Review Dedup Cafe", "+923001234567", "positive", None)

    def test_non_list_question_still_reaches_general_chat(self, store_id):
        from app.agents.reputation import process_reputation_owner_reply
        with patch("app.agents.reputation._classify_review_query", return_value=(None, None)), \
             patch("app.agents.reputation._chat_about_reviews", return_value="chatted") as mock_chat:
            reply = process_reputation_owner_reply("+923001234567", "how is our rating trending", store_id=store_id)
        mock_chat.assert_called_once()
        assert reply == "chatted"


# ── gateway/internal.py: router dispatch for reputation ──────────────────────

class TestInternalRoutingForReviewListing:
    def test_router_positive_command_passes_original_text_not_canonical_word(self, store_id):
        """Used to canonicalize down to the bare word "positive" --
        changed because that silently discarded a time modifier the
        original phrasing might carry ("show me LAST WEEK'S positive
        reviews"), with no way to recover it downstream once reputation.py
        only saw the bare word. Original text now always passed through
        for positive/negative/reviews (only "check" still canonicalizes)."""
        from app.gateway.internal import handle_internal_for_store
        with patch("app.gateway.internal._classify_with_llm", return_value=("reputation", "positive")), \
             patch("app.gateway.internal._reputation", return_value="ok") as mock_rep:
            handle_internal_for_store("+923001234567", "show me the good reviews", store_id)
        mock_rep.assert_called_once_with(store_id, "+923001234567", "show me the good reviews")

    def test_router_negative_command_passes_original_text_not_canonical_word(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        with patch("app.gateway.internal._classify_with_llm", return_value=("reputation", "negative")), \
             patch("app.gateway.internal._reputation", return_value="ok") as mock_rep:
            handle_internal_for_store("+923001234567", "what are people complaining about", store_id)
        mock_rep.assert_called_once_with(store_id, "+923001234567", "what are people complaining about")

    def test_router_reviews_command_passes_original_text_not_canonical_word(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        with patch("app.gateway.internal._classify_with_llm", return_value=("reputation", "reviews")), \
             patch("app.gateway.internal._reputation", return_value="ok") as mock_rep:
            handle_internal_for_store("+923001234567", "show me all the reviews", store_id)
        mock_rep.assert_called_once_with(store_id, "+923001234567", "show me all the reviews")

    def test_router_check_command_still_canonicalizes(self, store_id):
        """check has no useful modifiers, so it still canonicalizes to the
        bare command word for reputation.py's instant exact-match path."""
        from app.gateway.internal import handle_internal_for_store
        with patch("app.gateway.internal._classify_with_llm", return_value=("reputation", "check")), \
             patch("app.gateway.internal._reputation", return_value="ok") as mock_rep:
            handle_internal_for_store("+923001234567", "can you check our google reviews", store_id)
        mock_rep.assert_called_once_with(store_id, "+923001234567", "check")

    def test_router_chat_command_still_passes_original_text(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        with patch("app.gateway.internal._classify_with_llm", return_value=("reputation", "chat")), \
             patch("app.gateway.internal._reputation", return_value="ok") as mock_rep:
            handle_internal_for_store("+923001234567", "how is our rating trending", store_id)
        mock_rep.assert_called_once_with(store_id, "+923001234567", "how is our rating trending")

    def test_next_bypasses_llm_classifier_entirely(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        with patch("app.gateway.internal._classify_with_llm") as mock_classify, \
             patch("app.gateway.internal._reputation", return_value="ok") as mock_rep:
            handle_internal_for_store("+923001234567", "next", store_id)
        mock_classify.assert_not_called()
        mock_rep.assert_called_once()

    def test_staff_help_text_lists_the_new_commands(self):
        from app.gateway.internal import staff_help_text
        text = staff_help_text("Test Cafe").lower()
        assert "positive reviews" in text
        assert "negative reviews" in text
        assert "next" in text


# ── _format_pending: same Instagram-rating fix ───────────────────────────────

class TestFormatPendingRatingDisplay:
    def test_instagram_pending_does_not_say_no_rating(self):
        from app.agents.reputation import _format_pending
        pending = {
            "rating": None, "source_platform": "Instagram - anatummyisb comment",
            "content_text": "so good", "ai_summary": {"draft_reply": "Thanks!"},
        }
        result = _format_pending(pending)
        assert "no rating" not in result.lower()
        assert "*Instagram*" in result

    def test_google_maps_pending_still_says_no_rating_when_missing(self):
        from app.agents.reputation import _format_pending
        pending = {
            "rating": None, "source_platform": "Google Maps - Anatummy",
            "content_text": "decent", "ai_summary": {"draft_reply": "Thanks!"},
        }
        result = _format_pending(pending)
        assert "no rating" in result.lower()

    def test_rated_review_shows_stars_regardless_of_platform(self):
        from app.agents.reputation import _format_pending
        pending = {
            "rating": 5.0, "source_platform": "Google Maps - Anatummy",
            "content_text": "great", "ai_summary": {"draft_reply": "Thanks!"},
        }
        result = _format_pending(pending)
        assert "5.0/5" in result


# ── search_reviews_semantic: real embeddings, real similarity ───────────────
# Fixes a real gap: _chat_about_reviews used to only ever see the 10 most
# recent reviews (get_recent_reviews), so a free-form question about
# something an OLDER review mentioned (confirmed live: stores accumulate
# 300+ reviews) would never find it. This searches review TEXT content
# by meaning, not recency. Uses the real sentence-transformers model
# (already a project dependency) -- no mocking, since the whole point is
# verifying genuine semantic relevance, not just that a function returns
# something.

class TestSearchReviewsSemantic:
    def test_finds_the_semantically_relevant_review_not_just_keyword_match(self, store_id):
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        reviews = [
            ("parking", "Could not find anywhere to park, took forever."),
            ("coffee", "Best coffee in town, staff are lovely."),
            ("wifi", "The wifi kept dropping the whole time I was working."),
            ("cake", "The chocolate cake was rich and fresh."),
        ]
        for hash_, txt in reviews:
            review_db.save_review_finding(
                store_id, run_id, "Cafe", _raw_review(hash_, text=txt, rating=3.0), {"status": "auto_closed"},
            )

        # Deliberately no shared words with the "wifi" review's text --
        # a keyword/LIKE search would miss this, semantic search shouldn't.
        results = review_db.search_reviews_semantic(store_id, "internet connectivity problems", top_k=2)
        assert results
        assert results[0]["hash"] == "wifi"

    def test_irrelevant_query_returns_nothing_forced(self, store_id):
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        review_db.save_review_finding(
            store_id, run_id, "Cafe", _raw_review("h1", text="Great ambiance and friendly staff.", rating=5.0),
            {"status": "auto_closed"},
        )
        results = review_db.search_reviews_semantic(store_id, "staff uniform colors", top_k=5, min_similarity=0.5)
        assert results == []

    def test_finds_an_old_review_not_in_the_recent_sample(self, store_id):
        """The actual bug being fixed: an old review outside get_recent_
        reviews(limit=10)'s window must still be findable by content."""
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        review_db.save_review_finding(
            store_id, run_id, "Cafe",
            _raw_review("old_parking", text="Parking was impossible to find nearby.", rating=2.0),
            {"status": "auto_closed"},
        )
        # 12 newer reviews pushing the parking one out of a limit=10 recency window
        for i in range(12):
            review_db.save_review_finding(
                store_id, run_id, "Cafe", _raw_review(f"newer{i}", text=f"Nice visit number {i}.", rating=4.0),
                {"status": "auto_closed"},
            )
        recent = review_db.get_recent_reviews(store_id, limit=10)
        assert "old_parking" not in {r["hash"] for r in recent}  # confirms the gap exists

        results = review_db.search_reviews_semantic(store_id, "parking availability", top_k=3)
        assert any(r["hash"] == "old_parking" for r in results)

    def test_no_query_or_no_reviews_returns_empty_not_error(self, store_id):
        from app.review_sources import db as review_db
        assert review_db.search_reviews_semantic(store_id, "", top_k=5) == []
        assert review_db.search_reviews_semantic(store_id, "anything", top_k=5) == []  # no reviews seeded

    def test_sentiment_filter_excludes_topically_similar_wrong_polarity_matches(self, store_id):
        """The real gap found live: pure topical similarity matches SUBJECT,
        not polarity -- "complaints about the branch" surfaced praise
        ("Very nice amazing Branch") right alongside actual complaints,
        since both mention "branch". Confirmed against real production
        review data before this filter existed."""
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        seeds = [
            ("praise1", "Very nice amazing new branch, loved it!", "positive"),
            ("praise2", "The new branch opening is fantastic news.", "positive"),
            ("complaint1", "The new branch has terrible service, waited forever.", "negative"),
            ("complaint2", "New branch location has no parking, very disappointing.", "negative"),
        ]
        for hash_, text, sentiment in seeds:
            review_db.save_review_finding(
                store_id, run_id, "Cafe",
                _raw_review(hash_, text=text, rating=5.0 if sentiment == "positive" else 1.5),
                {"status": "auto_closed", "sentiment": sentiment},
            )

        unfiltered = review_db.search_reviews_semantic(store_id, "complaints about the branch", top_k=5)
        assert len(unfiltered) == 4  # the bug: both polarities match topically

        filtered = review_db.search_reviews_semantic(store_id, "complaints about the branch", top_k=5, sentiment="negative")
        assert {r["hash"] for r in filtered} == {"complaint1", "complaint2"}

    def test_no_sentiment_filter_when_none_passed(self, store_id):
        from app.review_sources import db as review_db
        run_id = _seed_run(store_id)
        review_db.save_review_finding(
            store_id, run_id, "Cafe", _raw_review("h1", text="Great branch experience overall.", rating=5.0),
            {"status": "auto_closed", "sentiment": "positive"},
        )
        results = review_db.search_reviews_semantic(store_id, "the branch", top_k=5, sentiment=None)
        assert len(results) == 1


class TestDetectSentimentLean:
    """Structural correctness with a mocked LLM response -- tests run
    with ZAI_API_KEY="" (conftest.py disables live calls by default).
    Real-model accuracy verified separately, live (confirmed: correctly
    detects "negative" for "complaints about the branch" and "positive"
    for "praise for the new branch" against the real API)."""

    def _mock_response(self, content):
        from unittest.mock import MagicMock
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value.choices[0].message.content = content
        return mock_client

    def test_returns_negative_when_llm_says_negative(self):
        from app.agents.reputation import _detect_sentiment_lean
        with patch("app.core.llm.get_client", return_value=self._mock_response("negative")), \
             patch("app.core.llm.get_fast_model", return_value="glm-4-plus"):
            assert _detect_sentiment_lean("complaints about the branch") == "negative"

    def test_returns_positive_when_llm_says_positive(self):
        from app.agents.reputation import _detect_sentiment_lean
        with patch("app.core.llm.get_client", return_value=self._mock_response("positive")), \
             patch("app.core.llm.get_fast_model", return_value="glm-4-plus"):
            assert _detect_sentiment_lean("praise for the new branch") == "positive"

    def test_returns_none_when_llm_says_none(self):
        from app.agents.reputation import _detect_sentiment_lean
        with patch("app.core.llm.get_client", return_value=self._mock_response("none")), \
             patch("app.core.llm.get_fast_model", return_value="glm-4-plus"):
            assert _detect_sentiment_lean("what do people say about the branch") is None

    def test_unrecognized_answer_falls_back_to_none(self):
        """Untrusted output -- anything that isn't literally 'positive'
        or 'negative' must degrade to no filter, not an error or a
        guessed value."""
        from app.agents.reputation import _detect_sentiment_lean
        with patch("app.core.llm.get_client", return_value=self._mock_response("maybe?")), \
             patch("app.core.llm.get_fast_model", return_value="glm-4-plus"):
            assert _detect_sentiment_lean("something") is None

    def test_no_api_key_returns_none_without_erroring(self):
        from app.agents.reputation import _detect_sentiment_lean
        # conftest.py already sets ZAI_API_KEY="" -- get_client() itself
        # will raise/misbehave without a key, confirming the except path works.
        assert _detect_sentiment_lean("complaints about the branch") is None


class TestDetectTimeRange:
    """Structural correctness with a mocked LLM response -- tests run
    with ZAI_API_KEY="" (conftest.py disables live calls by default).
    Real-model accuracy verified separately, live."""

    def _mock_response(self, content):
        from unittest.mock import MagicMock
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value.choices[0].message.content = content
        return mock_client

    def test_parses_a_valid_day_count(self):
        from app.agents.reputation import _detect_time_range
        with patch("app.core.llm.get_client", return_value=self._mock_response("7")), \
             patch("app.core.llm.get_fast_model", return_value="glm-4-plus"):
            assert _detect_time_range("last week's positive reviews") == 7

    def test_returns_none_when_llm_says_none(self):
        from app.agents.reputation import _detect_time_range
        with patch("app.core.llm.get_client", return_value=self._mock_response("none")), \
             patch("app.core.llm.get_fast_model", return_value="glm-4-plus"):
            assert _detect_time_range("positive reviews") is None

    def test_non_integer_answer_falls_back_to_none(self):
        """Untrusted output -- anything that doesn't parse as a positive
        integer must degrade to no time filter, not an error."""
        from app.agents.reputation import _detect_time_range
        with patch("app.core.llm.get_client", return_value=self._mock_response("a week")), \
             patch("app.core.llm.get_fast_model", return_value="glm-4-plus"):
            assert _detect_time_range("last week") is None

    def test_zero_or_negative_falls_back_to_none(self):
        from app.agents.reputation import _detect_time_range
        with patch("app.core.llm.get_client", return_value=self._mock_response("0")), \
             patch("app.core.llm.get_fast_model", return_value="glm-4-plus"):
            assert _detect_time_range("today") is None

    def test_empty_text_returns_none_without_calling_llm(self):
        from app.agents.reputation import _detect_time_range
        assert _detect_time_range("") is None
        assert _detect_time_range("   ") is None

    def test_no_api_key_returns_none_without_erroring(self):
        from app.agents.reputation import _detect_time_range
        assert _detect_time_range("last week's reviews") is None


class TestChatAboutReviewsBlending:
    def test_relevant_and_recent_are_merged_without_duplicates(self, store_id):
        from app.agents.reputation import _chat_about_reviews
        from app.review_sources import db as review_db
        from unittest.mock import MagicMock
        run_id = _seed_run(store_id)
        review_db.save_review_finding(
            store_id, run_id, "Cafe", _raw_review("parking", text="No parking spots ever available.", rating=2.0),
            {"status": "auto_closed"},
        )

        captured = {}
        def fake_create(*args, **kwargs):
            captured["messages"] = kwargs.get("messages")
            resp = MagicMock()
            resp.choices[0].message.content = "answer"
            return resp

        with patch("app.core.llm.get_client") as mock_get_client, \
             patch("app.core.llm.get_model", return_value="glm-4.7"), \
             patch("app.core.llm.nothink_kwargs", return_value={}):
            mock_client = MagicMock()
            mock_client.chat.completions.create.side_effect = fake_create
            mock_get_client.return_value = mock_client
            _chat_about_reviews(store_id, "Cafe", "any complaints about parking?")

        system_msg = captured["messages"][0]["content"]
        assert "MOST RELEVANT TO THE QUESTION" in system_msg
        assert "No parking spots ever available" in system_msg
        # Must appear exactly once, not duplicated across the relevant/recent sections
        assert system_msg.count("No parking spots ever available") == 1
