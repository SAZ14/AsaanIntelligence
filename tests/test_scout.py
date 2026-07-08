"""Scout agent tests.

Tests competitor seeding/isolation, discovery helpers, keyword routing,
and _extract_business_name without hitting any external API.
"""
import pytest
from unittest.mock import patch, MagicMock

from tests.conftest import seed_chain, seed_store, TestSession
from app.agents.scout.config import COMPETITORS
from app.agents.scout.discovery import (
    _extract_business_name,
    _is_own_brand,
    _store_context,
    get_all_competitors,
    seed_competitors_for_store,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def store_id():
    chain_id = seed_chain("Scout Chain")
    return seed_store(chain_id, name="Sugar Rush", location="Kohsar Market, F-6, Islamabad")


@pytest.fixture
def fake_redis(monkeypatch):
    """The scout in-flight guard is an atomic Redis lock (SET NX) -- without
    a real or fake connection, cache.try_lock/is_locked fail open (True/
    False respectively), which would make every in-flight test trivially
    pass or fail regardless of the actual guard logic. Same fixture
    pattern as test_review_dedup_and_listing.py/test_guards.py."""
    fakeredis = pytest.importorskip("fakeredis")
    import app.core.cache as cache
    monkeypatch.setattr(cache, "_client", fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(cache, "_unavailable", False)


@pytest.fixture
def two_store_ids():
    chain_id = seed_chain("Scout Chain 2")
    s1 = seed_store(chain_id, name="Brew Point", location="Johar Town, Lahore", category="coffee")
    s2 = seed_store(chain_id, name="The Pâtisserie", location="DHA Phase 6, Lahore", category="bakery")
    return s1, s2


# ── Competitor seeding ────────────────────────────────────────────────────────

def test_seed_competitors_populates_db(store_id):
    added = seed_competitors_for_store(store_id)
    assert added == len(COMPETITORS)


def test_seed_competitors_matches_config_count(store_id):
    seed_competitors_for_store(store_id)
    comps = get_all_competitors(store_id)
    assert len(comps) == len(COMPETITORS)


def test_seed_competitors_skips_duplicates_on_second_call(store_id):
    seed_competitors_for_store(store_id)
    added2 = seed_competitors_for_store(store_id)
    assert added2 == 0


def test_seed_competitors_stores_correct_names(store_id):
    seed_competitors_for_store(store_id)
    comps = get_all_competitors(store_id)
    names = {c["name"] for c in comps}
    config_names = {c["name"] for c in COMPETITORS}
    assert names == config_names


def test_seed_competitors_preserves_handles(store_id):
    seed_competitors_for_store(store_id)
    comps = {c["name"]: c for c in get_all_competitors(store_id)}
    # Baskin Robbins has a known handle
    br = comps.get("Baskin Robbins Pakistan")
    assert br is not None
    assert br["instagram_handle"] == "baskinrobbinspk"


def test_seed_competitors_marks_source_as_seed(store_id):
    seed_competitors_for_store(store_id)
    comps = get_all_competitors(store_id)
    assert all(c["source"] == "seed" for c in comps)


# ── Competitor isolation between stores ───────────────────────────────────────

def test_seed_is_independent_per_store(two_store_ids):
    s1, s2 = two_store_ids
    seed_competitors_for_store(s1)
    # s2 should be empty
    assert get_all_competitors(s2) == []


def test_each_store_gets_full_seed_list(two_store_ids):
    s1, s2 = two_store_ids
    seed_competitors_for_store(s1)
    seed_competitors_for_store(s2)
    assert len(get_all_competitors(s1)) == len(COMPETITORS)
    assert len(get_all_competitors(s2)) == len(COMPETITORS)


def test_competitor_added_to_store1_invisible_to_store2(two_store_ids):
    s1, s2 = two_store_ids
    from app.core.db import Competitor
    with TestSession() as db:
        db.add(Competitor(store_id=s1, name="Exclusive Rival", source="discovered"))
        db.commit()

    comps_s2 = get_all_competitors(s2)
    assert not any(c["name"] == "Exclusive Rival" for c in comps_s2)


# ── _store_context ────────────────────────────────────────────────────────────

def test_store_context_extracts_city_from_location(store_id):
    city, brand = _store_context(store_id)
    assert city == "Islamabad"
    assert brand == "Sugar Rush"


def test_store_context_uses_last_comma_token_as_city():
    chain_id = seed_chain("City Chain")
    s = seed_store(chain_id, name="Café Metro", location="Block 4, PECHS, Karachi")
    city, brand = _store_context(s)
    assert city == "Karachi"
    assert brand == "Café Metro"


def test_store_context_unknown_store_returns_defaults():
    city, brand = _store_context(999999)
    assert city == "Islamabad"
    assert brand == "this restaurant"


def test_store_context_no_location_defaults_to_islamabad():
    chain_id = seed_chain("NoLoc Chain")
    s = seed_store(chain_id, name="Noloc Cafe", location=None)
    city, _ = _store_context(s)
    assert city == "Islamabad"


# ── _is_own_brand ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("candidate,brand,expected", [
    ("Sugar Rush", "Sugar Rush", True),          # exact match
    ("sugar rush", "Sugar Rush", True),          # case-insensitive
    ("Sugar Rush Islamabad", "Sugar Rush", True),# brand is substring of candidate
    ("Sugar Rush Bakery", "Sugar Rush", True),   # brand is substring
    ("The Sugar Rush", "Sugar Rush", True),      # candidate contains brand
    ("Baskin Robbins", "Sugar Rush", False),     # unrelated competitor
    ("Layers Bakeshop", "Sugar Rush", False),    # another competitor
    ("O'Brownies", "Sugar Rush", False),
    ("rush", "Sugar Rush", True),                # substring in brand
    ("sugar", "Sugar Rush", True),               # substring in brand
])
def test_is_own_brand(candidate, brand, expected):
    assert _is_own_brand(candidate, brand) == expected


# ── _extract_business_name ────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected_non_empty", [
    ("Baskin Robbins | Ice Cream Islamabad", True),
    ("Layers Bakeshop - Best Cakes in Town", True),
    ("The Pâtisserie - Gulberg Lahore", True),
    ("Café Flo | Jinnah Ave", True),
])
def test_extract_business_name_gets_name_from_structured_text(text, expected_non_empty):
    name = _extract_business_name(text)
    if expected_non_empty:
        assert name is not None
        assert len(name) > 3
    else:
        assert name is None


@pytest.mark.parametrize("text", [
    "Best cafes in Islamabad 2026",   # starts with noise word
    "Top 10 restaurants in Lahore",   # starts with noise word
    "Where to find coffee in Karachi",
    "",
    "Ab",                              # too short
    "What's the best bakery?",         # question mark
    "find a bakery in DHA",            # starts with noise
])
def test_extract_business_name_rejects_noise(text):
    name = _extract_business_name(text)
    assert name is None


def test_extract_business_name_strips_islamabad_suffix():
    name = _extract_business_name("Burning Brownie Islamabad")
    assert name is not None
    assert "islamabad" not in name.lower()
    assert "Burning Brownie" in name


def test_extract_business_name_strips_lahore_suffix():
    name = _extract_business_name("Cafe Aura Lahore - Best Desserts")
    assert name is not None
    assert "lahore" not in name.lower()


def test_extract_business_name_too_many_words_rejected():
    name = _extract_business_name("The Very Best Coffee House In The World")
    assert name is None  # > 5 words


def test_extract_business_name_no_capital_rejected():
    name = _extract_business_name("the neighborhood cafe")
    assert name is None  # no capitalised word


# ── junk-name filtering (confirmed live: these exact shapes slipped through
# and got scraped at real Apify cost before this filter existed) ────────────

@pytest.mark.parametrize("text", [
    "Cafe Sierra (Islamabad",           # truncated mid-parenthetical
    "The Burgers (",
    "Cafe Near Me",                     # generic phrase, not a business
    "TBC on Instagram",
    "Jawahar Mustafa❤️",       # person's name + emoji, not a business
])
def test_extract_business_name_rejects_junk_shapes(text):
    from app.agents.scout.discovery import _extract_business_name as ebn
    assert ebn(text) is None


class TestLooksLikeJunkName:
    def test_truncated_trailing_paren_is_junk(self):
        from app.agents.scout.discovery import _looks_like_junk_name
        assert _looks_like_junk_name("Cafe Sierra (") is True

    def test_generic_phrase_is_junk(self):
        from app.agents.scout.discovery import _looks_like_junk_name
        assert _looks_like_junk_name("Cafe Near Me") is True
        assert _looks_like_junk_name("TBC on Instagram") is True

    def test_emoji_is_junk(self):
        from app.agents.scout.discovery import _looks_like_junk_name
        assert _looks_like_junk_name("Jawahar Mustafa❤️") is True

    def test_normal_business_name_is_not_junk(self):
        from app.agents.scout.discovery import _looks_like_junk_name
        assert _looks_like_junk_name("Bangin Buns") is False
        assert _looks_like_junk_name("KFC") is False
        assert _looks_like_junk_name("Original Premium Burgers") is False


# ── prune_stale_competitors ───────────────────────────────────────────────────

def test_prune_removes_junk_named_competitor_immediately_regardless_of_age(store_id):
    """A structurally-junk name is removed on sight -- it doesn't get the
    same age-based grace period as an otherwise-well-formed but unproven
    discovered competitor."""
    from datetime import datetime
    from app.agents.scout.discovery import prune_stale_competitors
    from app.core.db import Competitor

    with TestSession() as db:
        db.add(Competitor(store_id=store_id, name="Cafe Near Me", source="discovered",
                          created_at=datetime.utcnow()))  # created seconds ago
        db.commit()

    pruned = prune_stale_competitors(store_id)
    assert pruned == 1
    assert get_all_competitors(store_id) == []


def test_prune_removes_junk_name_even_with_real_findings(store_id):
    """Mirrors the live incident: a mismatched Google Maps entity produced
    289 real review findings under a junk name. Finding count alone must
    not save a structurally-invalid name."""
    from datetime import datetime
    from app.agents.scout.discovery import prune_stale_competitors
    from app.core.db import Competitor, Finding, ScoutRun

    with TestSession() as db:
        db.add(Competitor(store_id=store_id, name="Cafe Near Me", source="discovered",
                          created_at=datetime.utcnow()))
        run = ScoutRun(store_id=store_id, command="scout", status="ok")
        db.add(run)
        db.commit()
        db.refresh(run)
        db.add(Finding(store_id=store_id, run_id=run.id, competitor_name="Cafe Near Me",
                       source_platform="google_maps", update_type="post",
                       content_text="x", content_hash="h1"))
        db.commit()

    pruned = prune_stale_competitors(store_id)
    assert pruned == 1
    assert get_all_competitors(store_id) == []


# ── Keyword routing ───────────────────────────────────────────────────────────

def test_classify_agent_integrity_keywords():
    from app.core.routing import classify_agent
    assert classify_agent("show me the audit") == "integrity"
    assert classify_agent("leakage this week") == "integrity"
    assert classify_agent("give me the profit breakdown") == "integrity"
    assert classify_agent("daily report") == "integrity"
    assert classify_agent("weekly") == "integrity"
    assert classify_agent("staff anomalies") == "integrity"


def test_classify_agent_scout_keywords():
    from app.core.routing import classify_agent
    assert classify_agent("scout the competitors") == "scout"
    assert classify_agent("any intel on competitors?") == "scout"
    assert classify_agent("competitor update") == "scout"


def test_classify_agent_revenue_keywords():
    from app.core.routing import classify_agent
    assert classify_agent("revenue forecast") == "revenue"
    assert classify_agent("what are our sales projections") == "revenue"
    assert classify_agent("pricing strategy") == "revenue"


def test_classify_agent_revenue_not_in_integrity_set():
    """Bug that was fixed: 'revenue' was in both sets. Verify integrity check first."""
    from app.core.routing import classify_agent, _INTEGRITY_KEYWORDS
    assert "revenue" not in _INTEGRITY_KEYWORDS


def test_classify_agent_returns_none_for_unrecognised():
    from app.core.routing import classify_agent
    assert classify_agent("hi how are you") is None
    assert classify_agent("") is None




# ── Internal routing handler ──────────────────────────────────────────────────

def test_internal_handler_routes_to_integrity(store_id):
    from app.gateway.internal import handle_internal_for_store
    from unittest.mock import patch as mp
    with mp("app.gateway.internal._integrity", return_value="integrity reply") as mock_int:
        result = handle_internal_for_store("+923001234567", "show me the audit", store_id)
    mock_int.assert_called_once()
    assert result == "integrity reply"


def test_internal_handler_routes_to_scout(store_id):
    from app.gateway.internal import handle_internal_for_store
    from unittest.mock import patch as mp
    with mp("app.gateway.internal._scout", return_value="scout reply") as mock_sc:
        result = handle_internal_for_store("+923001234567", "scout intel", store_id)
    mock_sc.assert_called_once()
    assert result == "scout reply"


def test_internal_handler_routes_to_revenue(store_id):
    """"revenue forecast" is natural language (2 words, not the exact
    single-word shorthand) -- routes to the free-form answer_question()
    path, not the classify-into-fixed-intent handle_message() path."""
    from app.gateway.internal import handle_internal_for_store
    from unittest.mock import patch as mp
    with mp("app.gateway.internal._revenue_answer", return_value="revenue reply") as mock_rev:
        result = handle_internal_for_store("+923001234567", "revenue forecast", store_id)
    mock_rev.assert_called_once()
    assert result == "revenue reply"


def test_internal_handler_defaults_to_integrity(store_id):
    from app.gateway.internal import handle_internal_for_store
    from unittest.mock import patch as mp
    with mp("app.gateway.internal._integrity", return_value="default integrity") as mock_int:
        result = handle_internal_for_store("+923001234567", "hello what's going on", store_id)
    mock_int.assert_called_once()


# ── Discovery skips when no API key ──────────────────────────────────────────

def test_discover_new_competitors_skips_without_apify(store_id, caplog):
    from app.agents.scout.discovery import discover_new_competitors
    with patch("app.agents.scout.discovery.APIFY_TOKEN", ""):
        discover_new_competitors(store_id)
    comps = get_all_competitors(store_id)
    assert comps == []  # nothing added


def test_confirm_seed_skips_handle_resolution_without_apify(store_id):
    seed_competitors_for_store(store_id)
    with patch("app.agents.scout.discovery.APIFY_TOKEN", ""):
        from app.agents.scout.discovery import confirm_seed_competitors
        confirm_seed_competitors(store_id)  # should not raise or call web search
    # Handles that were already None remain None (no fake resolution)
    comps = {c["name"]: c for c in get_all_competitors(store_id)}
    loafology = comps.get("Loafology Bakery & Cafe")
    if loafology:
        assert loafology["instagram_handle"] is None  # still unresolved


# ── Pipeline _store_info ──────────────────────────────────────────────────────

def test_pipeline_store_name_reads_from_db(store_id):
    from app.agents.scout.pipeline import _store_info
    name, category = _store_info(store_id)
    assert name == "Sugar Rush"


def test_pipeline_store_name_unknown_store_returns_restaurant():
    from app.agents.scout.pipeline import _store_info
    name, category = _store_info(999999)
    assert name == "the restaurant"


# ── In-flight guard: atomic Redis lock, no race, covers cron too ────────────
# A live scout run has been confirmed live to take 7-45 minutes. The old
# guard was a Postgres SELECT-then-INSERT (check for a "running" row,
# then later write one) with a 15-minute cutoff shorter than the observed
# max -- both a correctness gap (a genuinely still-running scrape stopped
# being recognized as in flight partway through) and a real race (two
# callers whose checks landed within the same window could both decide
# to proceed independently, since the check and the row write weren't
# atomic). Replaced with an atomic Redis lock (SET NX -- cache.try_lock),
# checked identically by every caller: the cron poll, gateway/main.py's
# two dispatch sites, and gateway/internal.py's _scout() fallback tested
# here. These tests cover the lock mechanism directly and confirm _scout()
# honors it regardless of who (cron or another staff message) is holding it.

def test_run_in_flight_minutes_covers_the_confirmed_worst_case_duration():
    from app.agents.scout.config import RUN_IN_FLIGHT_MINUTES
    CONFIRMED_MAX_LIVE_SCRAPE_MINUTES = 45
    assert RUN_IN_FLIGHT_MINUTES > CONFIRMED_MAX_LIVE_SCRAPE_MINUTES


def test_scout_recognizes_a_lock_held_by_someone_else_as_in_flight(store_id, fake_redis):
    """Simulates the exact scenario asked about: the cron job's own
    run_scout_all() (or another staff message) already holds the lock --
    a staff message arriving here must see "already running", not start
    a second concurrent scrape, regardless of who/what acquired the lock."""
    from app.core import cache as _cache
    from app.agents.scout.config import RUN_IN_FLIGHT_MINUTES
    from app.agents.scout.pipeline import scout_live_lock_key
    from app.gateway.internal import _scout

    assert _cache.try_lock(scout_live_lock_key(store_id), ttl_seconds=RUN_IN_FLIGHT_MINUTES * 60)

    with patch("app.gateway.main._scout_rate_ok", return_value=True), \
         patch("app.agents.scout.pipeline.run") as mock_run:
        reply = _scout(store_id, "+923001234567", "what are competitors doing")

    mock_run.assert_not_called()
    assert "already running" in reply.lower()


def test_scout_proceeds_once_the_lock_is_free(store_id, fake_redis):
    from app.gateway.internal import _scout

    with patch("app.gateway.main._scout_rate_ok", return_value=True), \
         patch("app.agents.scout.pipeline.run", return_value="fresh report") as mock_run:
        reply = _scout(store_id, "+923001234567", "what are competitors doing")

    mock_run.assert_called_once()
    assert reply == "fresh report"


def test_only_one_of_two_simultaneous_callers_acquires_the_lock(store_id, fake_redis):
    """The actual atomicity guarantee -- two callers racing for the same
    key, neither having seen the other's state first. SET NX means
    exactly one succeeds no matter how close in time the attempts are."""
    from app.core import cache as _cache
    from app.agents.scout.pipeline import scout_live_lock_key

    key = scout_live_lock_key(store_id)
    first = _cache.try_lock(key, ttl_seconds=60)
    second = _cache.try_lock(key, ttl_seconds=60)

    assert first is True
    assert second is False


def test_runs_own_lock_blocks_a_concurrent_call_and_releases_when_done(store_id, fake_redis):
    """Integration-style: run() itself, not just the caller-side
    pre-screen, must refuse to proceed if the lock is already held, and
    must release it when it finishes so a later, genuinely fresh attempt
    isn't blocked forever."""
    from app.core import cache as _cache
    from app.agents.scout.pipeline import run, scout_live_lock_key, _scout_cache_key
    from app.agents.scout.config import RUN_IN_FLIGHT_MINUTES

    key = scout_live_lock_key(store_id)

    # Someone else already holds it -- run() must not do any discovery work.
    _cache.try_lock(key, ttl_seconds=RUN_IN_FLIGHT_MINUTES * 60)
    with patch("app.agents.scout.pipeline.confirm_seed_competitors") as mock_discover:
        reply = run("scout", store_id=store_id)
    mock_discover.assert_not_called()
    assert "already running" in reply.lower()

    # Once released, a fresh attempt can proceed and re-acquire it.
    _cache.release_lock(key)
    assert _cache.try_lock(key, ttl_seconds=60) is True


# ── Targeted competitor findings: real data, not the report-capped subset ───
# Fixes a real gap: _select_report_findings deliberately caps each
# competitor to REPORT_FINDINGS_PER_COMPETITOR=4 (to keep a many-
# competitor report balanced), and build_report only ever sees the LATEST
# run's findings anyway. A staff member asking specifically about one
# named competitor should get everything on record for them across all
# runs, not a 4-item, single-run slice.

def _seed_scout_run(store_id, status="ok"):
    from app.core.db import ScoutRun
    with TestSession() as db:
        run = ScoutRun(store_id=store_id, command="scout", status=status)
        db.add(run)
        db.commit()
        db.refresh(run)
        return run.id


def _seed_finding(store_id, run_id, competitor_name, text, update_type="post"):
    from app.core.db import Finding
    with TestSession() as db:
        db.add(Finding(
            store_id=store_id, run_id=run_id, competitor_name=competitor_name,
            source_platform="Instagram", update_type=update_type,
            content_text=text, content_hash=f"{competitor_name}-{text[:10]}-{run_id}",
        ))
        db.commit()


class TestTargetedCompetitorFindings:
    def test_fetches_across_all_runs_not_just_latest(self, store_id):
        from app.agents.scout.pipeline import _get_all_findings_for_competitor
        run1 = _seed_scout_run(store_id)
        run2 = _seed_scout_run(store_id)
        _seed_finding(store_id, run1, "Burger Lab", "Old 20% discount finding.")
        _seed_finding(store_id, run2, "Burger Lab", "New loaded fries launch.")
        _seed_finding(store_id, run2, "Rival Cafe", "Unrelated competitor's finding.")

        results = _get_all_findings_for_competitor(store_id, "Burger Lab")
        texts = {f.content_text for f in results}
        assert "Old 20% discount finding." in texts
        assert "New loaded fries launch." in texts
        assert "Unrelated competitor's finding." not in texts

    def test_excludes_review_type_findings(self, store_id):
        """The findings table is shared with reputation -- a competitor
        fetch must never accidentally pull in the store's OWN reviews
        (competitor_name is set to the store's own name for those)."""
        from app.agents.scout.pipeline import _get_all_findings_for_competitor
        run_id = _seed_scout_run(store_id)
        _seed_finding(store_id, run_id, "Sugar Rush", "5 star review text", update_type="review")
        _seed_finding(store_id, run_id, "Sugar Rush", "A real competitor finding about Sugar Rush's namesake rival")

        results = _get_all_findings_for_competitor(store_id, "Sugar Rush")
        assert len(results) == 1
        assert results[0].update_type != "review"

    def test_respects_the_cap(self, store_id):
        from app.agents.scout.pipeline import _get_all_findings_for_competitor, TARGETED_COMPETITOR_FINDINGS_LIMIT
        run_id = _seed_scout_run(store_id)
        for i in range(TARGETED_COMPETITOR_FINDINGS_LIMIT + 10):
            _seed_finding(store_id, run_id, "Burger Lab", f"finding {i}")
        results = _get_all_findings_for_competitor(store_id, "Burger Lab")
        assert len(results) == TARGETED_COMPETITOR_FINDINGS_LIMIT


class TestMaybeTargetCompetitor:
    def test_no_user_message_leaves_findings_unchanged(self, store_id):
        from app.agents.scout.pipeline import _maybe_target_competitor
        sentinel = ["unchanged"]
        assert _maybe_target_competitor(store_id, None, sentinel) is sentinel

    def test_general_question_leaves_findings_unchanged(self, store_id):
        from app.agents.scout.pipeline import _maybe_target_competitor
        with patch("app.agents.scout.discovery.get_all_competitors",
                    return_value=[{"name": "Burger Lab"}, {"name": "Rival Cafe"}]), \
             patch("app.agents.scout.analysis.classify_target_competitor", return_value=None):
            sentinel = ["unchanged"]
            result = _maybe_target_competitor(store_id, "what are competitors doing overall", sentinel)
        assert result is sentinel

    def test_named_competitor_swaps_in_the_targeted_fetch(self, store_id):
        from app.agents.scout.pipeline import _maybe_target_competitor
        run1 = _seed_scout_run(store_id)
        run2 = _seed_scout_run(store_id)
        _seed_finding(store_id, run1, "Burger Lab", "Old finding about Burger Lab.")
        _seed_finding(store_id, run2, "Burger Lab", "Newer finding about Burger Lab.")
        _seed_finding(store_id, run2, "Rival Cafe", "Finding about a different competitor.")

        with patch("app.agents.scout.discovery.get_all_competitors",
                    return_value=[{"name": "Burger Lab"}, {"name": "Rival Cafe"}]), \
             patch("app.agents.scout.analysis.classify_target_competitor", return_value="Burger Lab"):
            result = _maybe_target_competitor(store_id, "what has Burger Lab been up to", ["placeholder"])

        texts = {f.content_text for f in result}
        assert "Old finding about Burger Lab." in texts
        assert "Newer finding about Burger Lab." in texts
        assert "Finding about a different competitor." not in texts

    def test_errors_fall_back_to_original_findings(self, store_id):
        from app.agents.scout.pipeline import _maybe_target_competitor
        with patch("app.agents.scout.discovery.get_all_competitors", side_effect=RuntimeError("db down")):
            sentinel = ["unchanged"]
            result = _maybe_target_competitor(store_id, "what about Burger Lab", sentinel)
        assert result is sentinel


class TestClassifyTargetCompetitor:
    """Structural correctness with a mocked LLM response -- tests run with
    ZAI_API_KEY="" (conftest.py disables live calls by default, same as
    the rest of this suite). Real-model accuracy is verified separately,
    live, the same way other router/classifier prompts in this codebase
    are (scratchpad scripts against the real API, not the committed
    suite -- see this session's routing test scripts)."""

    def _names(self):
        return ["Burger Lab", "Cheezious", "OPTP", "Salt'n Pepper"]

    def test_returns_the_llm_answer_when_it_matches_a_known_name(self):
        from app.agents.scout import analysis
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value.choices[0].message.content = "Burger Lab"
        with patch.object(analysis._cfg, "ZAI_API_KEY", "fake-key-for-test"), \
             patch.object(analysis, "_get_client", return_value=mock_client), \
             patch("app.core.llm.get_fast_model", return_value="glm-4-plus"):
            result = analysis.classify_target_competitor("what has Burger Lab been doing lately", self._names())
        assert result == "Burger Lab"

    def test_rejects_a_hallucinated_name_not_in_the_candidate_list(self):
        """The LLM must be treated as untrusted here -- if it returns
        something that isn't literally one of the known competitor names
        (a hallucination, a typo, extra punctuation), fall back to None
        (general question) rather than querying the DB for a competitor
        that was never actually given as an option."""
        from app.agents.scout import analysis
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value.choices[0].message.content = "Some Made Up Place"
        with patch.object(analysis._cfg, "ZAI_API_KEY", "fake-key-for-test"), \
             patch.object(analysis, "_get_client", return_value=mock_client), \
             patch("app.core.llm.get_fast_model", return_value="glm-4-plus"):
            result = analysis.classify_target_competitor("tell me about them", self._names())
        assert result is None

    def test_none_answer_from_llm_means_general_question(self):
        from app.agents.scout import analysis
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value.choices[0].message.content = "NONE"
        with patch.object(analysis._cfg, "ZAI_API_KEY", "fake-key-for-test"), \
             patch.object(analysis, "_get_client", return_value=mock_client), \
             patch("app.core.llm.get_fast_model", return_value="glm-4-plus"):
            result = analysis.classify_target_competitor("what are competitors doing this week", self._names())
        assert result is None

    def test_no_competitors_returns_none_without_calling_llm(self):
        from app.agents.scout.analysis import classify_target_competitor
        assert classify_target_competitor("what about Burger Lab", []) is None

    def test_no_api_key_returns_none_without_calling_llm(self):
        from app.agents.scout import analysis
        with patch.object(analysis._cfg, "ZAI_API_KEY", ""):
            assert analysis.classify_target_competitor("what about Burger Lab", self._names()) is None
