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


# ── gateway/internal.py: router passes the canonical command through ────────

class TestInternalRoutingForReviewListing:
    def test_router_positive_command_passed_through_verbatim(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        with patch("app.gateway.internal._classify_with_llm", return_value=("reputation", "positive")), \
             patch("app.gateway.internal._reputation", return_value="ok") as mock_rep:
            handle_internal_for_store("+923001234567", "show me the good reviews", store_id)
        mock_rep.assert_called_once_with(store_id, "+923001234567", "positive")

    def test_router_negative_command_passed_through_verbatim(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        with patch("app.gateway.internal._classify_with_llm", return_value=("reputation", "negative")), \
             patch("app.gateway.internal._reputation", return_value="ok") as mock_rep:
            handle_internal_for_store("+923001234567", "what are people complaining about", store_id)
        mock_rep.assert_called_once_with(store_id, "+923001234567", "negative")

    def test_router_reviews_command_passed_through_verbatim(self, store_id):
        from app.gateway.internal import handle_internal_for_store
        with patch("app.gateway.internal._classify_with_llm", return_value=("reputation", "reviews")), \
             patch("app.gateway.internal._reputation", return_value="ok") as mock_rep:
            handle_internal_for_store("+923001234567", "show me all the reviews", store_id)
        mock_rep.assert_called_once_with(store_id, "+923001234567", "reviews")

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
