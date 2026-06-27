"""Scout agent tests.

Tests competitor seeding/isolation, discovery helpers, keyword routing,
and _extract_business_name without hitting any external API.
"""
import pytest
from unittest.mock import patch

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
    assert city == "city"
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


def test_is_switch_intent():
    from app.core.routing import is_switch_intent
    # is_switch_intent checks {"switch","change","swap","different","other"}
    # The "menu"/"back"/"home" reset is handled separately in gateway/main.py
    assert is_switch_intent("switch agents")
    assert is_switch_intent("change to revenue")
    assert is_switch_intent("swap")
    assert is_switch_intent("different agent please")
    assert not is_switch_intent("summary")
    assert not is_switch_intent("scout")
    assert not is_switch_intent("menu")  # handled by _MODE_TRIGGERS, not this fn


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
    from app.gateway.internal import handle_internal_for_store
    from unittest.mock import patch as mp
    with mp("app.gateway.internal._revenue", return_value="revenue reply") as mock_rev:
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


# ── Pipeline _store_name ──────────────────────────────────────────────────────

def test_pipeline_store_name_reads_from_db(store_id):
    from app.agents.scout.pipeline import _store_name
    name = _store_name(store_id)
    assert name == "Sugar Rush"


def test_pipeline_store_name_unknown_store_returns_restaurant():
    from app.agents.scout.pipeline import _store_name
    assert _store_name(999999) == "Restaurant"
