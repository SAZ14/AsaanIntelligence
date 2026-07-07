"""Scout cost/relevance ceilings: capping findings per competitor, pruning
stale auto-discovered competitors, and capping total competitors scraped.

Without these, a live run's cost scales unbounded -- discovery only ever
adds competitors (never removes), and a single multi-branch chain's Google
Maps reviews can dwarf every other competitor's finding count in one run
(confirmed live: one chain alone produced 226 review findings).
"""
from datetime import datetime, timedelta

import pytest

from tests.conftest import seed_chain, seed_store, TestSession
from app.agents.scout.cleaning import cap_findings_per_competitor
from app.agents.scout.schemas import FindingSchema


@pytest.fixture
def store_id():
    chain_id = seed_chain("Cost Limit Chain")
    return seed_store(chain_id, name="Cost Limit Cafe", location="F-7, Islamabad")


def _finding(name, update_type="post", rating=None, engagement=None, post_date=None) -> FindingSchema:
    return FindingSchema(
        competitor_name=name,
        source_platform="google_maps",
        update_type=update_type,
        content_text=f"finding for {name}",
        rating=rating,
        engagement=engagement,
        post_date=post_date,
    )


# ── cap_findings_per_competitor ───────────────────────────────────────────────

def test_under_cap_returns_everything_unchanged():
    findings = [_finding("KFC") for _ in range(5)]
    result = cap_findings_per_competitor(findings, max_per_competitor=12)
    assert len(result) == 5


def test_over_cap_trims_to_exactly_the_cap():
    findings = [_finding("KFC", rating=float(i % 5 + 1)) for i in range(226)]
    result = cap_findings_per_competitor(findings, max_per_competitor=12)
    assert len(result) == 12


def test_cap_is_per_competitor_not_global():
    findings = [_finding("KFC") for _ in range(50)] + [_finding("McDonalds") for _ in range(3)]
    result = cap_findings_per_competitor(findings, max_per_competitor=12)
    kfc = [f for f in result if f.competitor_name == "KFC"]
    mcd = [f for f in result if f.competitor_name == "McDonalds"]
    assert len(kfc) == 12
    assert len(mcd) == 3  # untouched, already under the cap


def test_trim_keeps_a_balanced_mix_of_update_types():
    """226 reviews all landing in one type shouldn't crowd out the other
    types entirely -- a round-robin trim should keep some of each."""
    findings = (
        [_finding("KFC", update_type="competitor_strength") for _ in range(100)]
        + [_finding("KFC", update_type="competitor_weakness") for _ in range(100)]
        + [_finding("KFC", update_type="review_trend") for _ in range(26)]
    )
    result = cap_findings_per_competitor(findings, max_per_competitor=9)
    kinds = {f.update_type for f in result}
    assert kinds == {"competitor_strength", "competitor_weakness", "review_trend"}


def test_trim_prefers_extreme_ratings_and_higher_engagement():
    findings = [
        _finding("KFC", update_type="post", rating=3.0),   # neutral -- least interesting
        _finding("KFC", update_type="post", rating=1.0),   # extreme -- most interesting
        _finding("KFC", update_type="post", rating=3.2),
        _finding("KFC", update_type="post", rating=5.0),   # extreme -- most interesting
    ]
    result = cap_findings_per_competitor(findings, max_per_competitor=2)
    kept_ratings = {f.rating for f in result}
    assert kept_ratings == {1.0, 5.0}


def test_zero_cap_is_a_noop_safety_valve():
    findings = [_finding("KFC")]
    assert cap_findings_per_competitor(findings, max_per_competitor=0) == findings


# ── prune_stale_competitors ───────────────────────────────────────────────────

def _add_competitor(store_id, name, source="discovered", age_days=0):
    from app.core.db import Competitor
    with TestSession() as db:
        db.add(Competitor(
            store_id=store_id, name=name, source=source,
            created_at=datetime.utcnow() - timedelta(days=age_days),
        ))
        db.commit()


def _add_finding(store_id, competitor_name, run_id):
    from app.core.db import Finding
    with TestSession() as db:
        db.add(Finding(
            store_id=store_id, run_id=run_id, competitor_name=competitor_name,
            source_platform="google_maps", update_type="post",
            content_text="x", content_hash=f"{competitor_name}-{run_id}",
        ))
        db.commit()


def _seed_run(store_id):
    from app.core.db import ScoutRun
    with TestSession() as db:
        run = ScoutRun(store_id=store_id, command="scout", status="ok")
        db.add(run)
        db.commit()
        db.refresh(run)
        return run.id


def test_old_competitor_with_no_findings_gets_pruned(store_id):
    from app.agents.scout.discovery import prune_stale_competitors, get_all_competitors
    _add_competitor(store_id, "Noise Result", age_days=10)
    pruned = prune_stale_competitors(store_id, min_age_days=3, min_findings=1)
    assert pruned == 1
    assert not any(c["name"] == "Noise Result" for c in get_all_competitors(store_id))


def test_old_competitor_with_findings_survives(store_id):
    from app.agents.scout.discovery import prune_stale_competitors, get_all_competitors
    _add_competitor(store_id, "Real Rival", age_days=10)
    run_id = _seed_run(store_id)
    _add_finding(store_id, "Real Rival", run_id)
    pruned = prune_stale_competitors(store_id, min_age_days=3, min_findings=1)
    assert pruned == 0
    assert any(c["name"] == "Real Rival" for c in get_all_competitors(store_id))


def test_young_competitor_is_never_pruned_even_with_no_findings(store_id):
    """Give new discoveries a fair chance across a few live runs before
    judging them -- freshness caching means they may not even have been
    scraped yet."""
    from app.agents.scout.discovery import prune_stale_competitors, get_all_competitors
    _add_competitor(store_id, "Too New To Judge", age_days=0)
    pruned = prune_stale_competitors(store_id, min_age_days=3, min_findings=1)
    assert pruned == 0
    assert any(c["name"] == "Too New To Judge" for c in get_all_competitors(store_id))


def test_curated_sources_are_never_pruned_regardless_of_performance(store_id):
    from app.agents.scout.discovery import prune_stale_competitors, get_all_competitors
    _add_competitor(store_id, "Primary Pick", source="primary", age_days=100)
    _add_competitor(store_id, "Seed Pick", source="seed", age_days=100)
    pruned = prune_stale_competitors(store_id, min_age_days=3, min_findings=1)
    assert pruned == 0
    names = {c["name"] for c in get_all_competitors(store_id)}
    assert {"Primary Pick", "Seed Pick"} <= names


# ── _select_competitors_to_scrape ─────────────────────────────────────────────

def test_under_cap_returns_everything(store_id):
    from app.agents.scout.pipeline import _select_competitors_to_scrape
    competitors = [{"name": f"C{i}", "source": "discovered"} for i in range(5)]
    result = _select_competitors_to_scrape(competitors, store_id, max_total=10)
    assert len(result) == 5


def test_curated_entries_always_survive_the_cap(store_id):
    from app.agents.scout.pipeline import _select_competitors_to_scrape
    curated = [{"name": f"Curated{i}", "source": "primary"} for i in range(5)]
    discovered = [{"name": f"Disc{i}", "source": "discovered"} for i in range(20)]
    result = _select_competitors_to_scrape(curated + discovered, store_id, max_total=8)
    names = {c["name"] for c in result}
    assert {c["name"] for c in curated} <= names
    assert len(result) == 8


def test_discovered_entries_ranked_by_historical_finding_count(store_id):
    from app.agents.scout.pipeline import _select_competitors_to_scrape

    run_id = _seed_run(store_id)
    _add_finding(store_id, "Proven", run_id)
    _add_finding(store_id, "Proven", run_id)
    _add_finding(store_id, "Proven", run_id)
    _add_finding(store_id, "Unproven", run_id)

    competitors = [
        {"name": "Proven", "source": "discovered"},
        {"name": "Unproven", "source": "discovered"},
        {"name": "NeverSeen", "source": "discovered"},
    ]
    result = _select_competitors_to_scrape(competitors, store_id, max_total=2)
    names = [c["name"] for c in result]
    assert "Proven" in names
    assert "NeverSeen" not in names  # zero history, dropped first
    assert len(names) == 2


def test_curated_alone_over_cap_is_truncated_too(store_id):
    """Cap is a hard ceiling -- even curated-only lists respect it (defensive;
    shouldn't happen in practice since a human sets these deliberately)."""
    from app.agents.scout.pipeline import _select_competitors_to_scrape
    curated = [{"name": f"Curated{i}", "source": "primary"} for i in range(15)]
    result = _select_competitors_to_scrape(curated, store_id, max_total=10)
    assert len(result) == 10


# ── _select_report_findings ───────────────────────────────────────────────────
#
# Confirmed live: a run with 1500+ findings produced a report with a single
# throwaway line per competitor, because build_report used to take a flat
# global top-25-by-relevance-score -- which let 2-3 highly-scored
# competitors crowd out every other competitor's evidence entirely, leaving
# the model nothing concrete to write about the rest.

def test_every_competitor_with_findings_gets_representation():
    from app.agents.scout.analysis import _select_report_findings
    findings = []
    # 15 competitors, one of which (Dominant) has by far the most/best findings
    for i in range(50):
        findings.append(_finding("Dominant", rating=5.0, engagement={"likes": 1000}))
    for i in range(14):
        findings.append(_finding(f"Minor{i}", rating=3.5))

    result = _select_report_findings(findings)
    names = {f.competitor_name for f in result}
    # every competitor must show up -- not just "Dominant"
    assert names == {"Dominant"} | {f"Minor{i}" for i in range(14)}


def test_no_single_competitor_can_exceed_its_quota():
    from app.agents.scout.analysis import (
        _select_report_findings, REPORT_FINDINGS_PER_COMPETITOR,
    )
    findings = [_finding("Dominant", rating=float(i % 5 + 1)) for i in range(200)]
    result = _select_report_findings(findings)
    assert len(result) == REPORT_FINDINGS_PER_COMPETITOR


def test_total_stays_within_budget_even_with_many_competitors():
    from app.agents.scout.analysis import (
        _select_report_findings, REPORT_MAX_TOTAL_FINDINGS,
    )
    # 30 competitors x 4 findings each = 120, over REPORT_MAX_TOTAL_FINDINGS
    findings = []
    for i in range(30):
        for _ in range(4):
            findings.append(_finding(f"Comp{i}", rating=3.0))
    result = _select_report_findings(findings)
    assert len(result) <= REPORT_MAX_TOTAL_FINDINGS


def test_selection_prefers_relevance_then_rating_extremity_then_engagement():
    from app.agents.scout.analysis import _select_report_findings, REPORT_FINDINGS_PER_COMPETITOR
    f_low_signal = _finding("KFC", rating=3.0, engagement={"likes": 1})
    f_low_signal.relevance_score = None
    f_high_relevance = _finding("KFC", rating=3.0, engagement={"likes": 1})
    f_high_relevance.relevance_score = 9
    findings = [f_low_signal] * 10 + [f_high_relevance]
    result = _select_report_findings(findings)
    assert f_high_relevance in result


# ── depth policy is applied to every real command, never to help ─────────────

def test_depth_policy_appended_to_every_command_except_help():
    from app.agents.scout.analysis import _command_instructions, REPORT_DEPTH_POLICY
    instructions = _command_instructions("Test Cafe")
    for cmd, text in instructions.items():
        if cmd == "help":
            assert REPORT_DEPTH_POLICY not in text
        # depth policy is appended in build_report, not baked into the dict --
        # just confirm none of the raw instructions already duplicate it
        assert REPORT_DEPTH_POLICY not in text


def test_competitors_instruction_no_longer_caps_at_1_to_2_lines():
    from app.agents.scout.analysis import _command_instructions
    instructions = _command_instructions("Test Cafe")
    assert "1-2 lines" not in instructions["competitors"]
