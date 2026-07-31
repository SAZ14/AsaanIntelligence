"""Golden scenarios + scorer for the Competitive Intelligence agent.

Each `EvalScenario` describes a competitive landscape (home menu, a previous
capture, and the current capture) plus an `Expectation` of what a competent
agent must conclude. `evaluate()` runs the *real* agent path (deterministic,
client=None) and returns the list of unmet expectations — empty means pass.

Everything here is deterministic and offline so it can gate CI. Scenarios are
intentionally single-signal where possible, so a failure points at one thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.agents.competitive_intel import run_competitive_agent
from app.analysis.competitive import CompetitiveReport
from app.models.canonical import MenuItem
from app.models.competitive import (
    Competitor,
    CompetitorMenuItem,
    CompetitorPromotion,
    CompetitorSnapshot,
    ReviewStanding,
    Scope,
)

NOW = datetime(2026, 6, 16, 9, 0)
PREV = datetime(2026, 5, 16, 9, 0)


# ── In-memory backends (no disk / network) ──

class _ListScraper:
    """Returns a fixed list of snapshots regardless of targets/scope."""

    def __init__(self, snapshots: list[CompetitorSnapshot]) -> None:
        self._snaps = snapshots

    def scrape(self, targets, scope) -> list[CompetitorSnapshot]:
        return self._snaps


class _SeededStore:
    """Serves a fixed baseline; ignores saves."""

    def __init__(self, previous: dict[str, CompetitorSnapshot]) -> None:
        self._prev = previous

    def save(self, snapshots) -> None:
        return None

    def latest_before(self, scope, run_captured_at) -> dict[str, CompetitorSnapshot]:
        return self._prev


# ── Builders ──

def _menu(*items: tuple[str, str, float]) -> dict[str, MenuItem]:
    return {
        name[:6].upper() + str(i): MenuItem(sku=name[:6].upper() + str(i), name=name, category=cat, price=price)
        for i, (name, cat, price) in enumerate(items)
    }


def _snap(cid, name, area, menu, reviews, captured, *, opened=None, promos=None,
          city="Islamabad", country="Pakistan") -> CompetitorSnapshot:
    return CompetitorSnapshot(
        competitor=Competitor(
            competitor_id=cid, name=name, area=area, city=city,
            country=country, opened_at=opened,
        ),
        captured_at=captured,
        menu=[CompetitorMenuItem(name=n, category=c, price=p, tags=list(t))
              for (n, c, p, t) in menu],
        promotions=[CompetitorPromotion(**pr) for pr in (promos or [])],
        reviews=[ReviewStanding(source=s, rating_avg=r, review_count=n) for (s, r, n) in reviews],
    )


# ── Expectation model ──

@dataclass
class Expectation:
    pricing: dict[str, str] = field(default_factory=dict)        # category -> recommendation
    must_detect_new_dishes: list[str] = field(default_factory=list)
    must_flag_new_venue: list[str] = field(default_factory=list)  # competitor names
    rising_competitors: list[str] = field(default_factory=list)
    menu_gap_categories: list[str] = field(default_factory=list)
    national_trend_labels: list[str] = field(default_factory=list)
    headline_must_mention: list[str] = field(default_factory=list)  # case-insensitive substrings


@dataclass
class EvalScenario:
    name: str
    description: str
    home_menu: dict[str, MenuItem]
    current: list[CompetitorSnapshot]
    previous: dict[str, CompetitorSnapshot]
    scope: Scope
    expect: Expectation


# ── Scorer ──

def run_scenario(scenario: EvalScenario) -> CompetitiveReport:
    return run_competitive_agent(
        scenario.home_menu,
        scope=scenario.scope,
        targets=[],
        scraper=_ListScraper(scenario.current),
        store=_SeededStore(scenario.previous),
        client=None,            # deterministic fallback phrasing
        persist=False,
    )


def evaluate(scenario: EvalScenario) -> list[str]:
    """Return a list of unmet expectations (empty list == pass)."""
    r = run_scenario(scenario)
    e = scenario.expect
    fails: list[str] = []

    # Pricing recommendations
    pricing_by_cat = {p.category: p.recommendation for p in r.pricing}
    for cat, want in e.pricing.items():
        got = pricing_by_cat.get(cat)
        if got != want:
            fails.append(f"pricing[{cat}]: expected {want!r}, got {got!r}")

    # New dishes detected
    new_names = {d.dish_name for d in r.new_dishes}
    for dish in e.must_detect_new_dishes:
        if dish not in new_names:
            fails.append(f"new dish not detected: {dish!r}")

    # New-venue flagging
    flagged_venues = {d.competitor_name for d in r.new_dishes if d.is_new_venue}
    for venue in e.must_flag_new_venue:
        if venue not in flagged_venues:
            fails.append(f"new venue not flagged: {venue!r}")

    # Review momentum direction
    rising = {t.competitor_name for t in r.review_trends if t.direction == "rising"}
    for venue in e.rising_competitors:
        if venue not in rising:
            fails.append(f"competitor not rising: {venue!r}")

    # Menu gaps
    gap_cats = {g.category for g in r.menu_gaps}
    for cat in e.menu_gap_categories:
        if cat not in gap_cats:
            fails.append(f"menu gap not found: {cat!r}")

    # National trends
    trend_labels = {t.label for t in r.national_trends}
    for label in e.national_trend_labels:
        if label not in trend_labels:
            fails.append(f"national trend not found: {label!r}")

    # Headlines must mention key facts
    blob = " || ".join(r.headlines).lower()
    for needle in e.headline_must_mention:
        if needle.lower() not in blob:
            fails.append(f"headline missing mention: {needle!r}  (headlines: {r.headlines})")

    return fails


# ── Scenarios ──

def _scenarios() -> list[EvalScenario]:
    scenarios: list[EvalScenario] = []

    # 1) Pricing headroom: we sit well below the area average on coffee.
    home = _menu(("Espresso", "Coffee", 400), ("Latte", "Coffee", 500), ("Cappuccino", "Coffee", 600))
    rival = _snap("r1", "Brew Lab", "F-7", [
        ("House Blend", "Coffee", 800, []),
        ("Cold Brew", "Coffee", 820, []),
        ("Flat White", "Coffee", 840, []),
    ], [("Google", 4.5, 200)], NOW)
    scenarios.append(EvalScenario(
        name="pricing_headroom",
        description="Coffee priced ~40% under the area average should recommend a raise.",
        home_menu=home,
        current=[rival],
        previous={"r1": rival},   # identical baseline => no new-dish noise
        scope=Scope.LOCAL,
        expect=Expectation(
            pricing={"coffee": "raise"},
            headline_must_mention=["below the area average"],
        ),
    ))

    # 2) The new spot in F-7 launches a viral dish.
    home2 = _menu(("Espresso", "Coffee", 500))
    new_spot = _snap("crust", "Crust & Co", "F-7", [
        ("Nashville Hot Sandwich", "Food", 1600, ["viral", "spicy"]),
        ("Korean Corn Dog", "Food", 1200, ["viral", "korean"]),
    ], [("Google", 4.8, 130)], NOW, opened=datetime.now() - timedelta(days=12))
    scenarios.append(EvalScenario(
        name="new_spot_in_f7",
        description="A brand-new F-7 venue must be flagged with its launch dish in the headline.",
        home_menu=home2,
        current=[new_spot],
        previous={},   # no baseline => genuinely new venue
        scope=Scope.LOCAL,
        expect=Expectation(
            must_flag_new_venue=["Crust & Co"],
            must_detect_new_dishes=["Nashville Hot Sandwich", "Korean Corn Dog"],
            headline_must_mention=["Crust & Co", "F-7"],
        ),
    ))

    # 3) Review momentum: a rival's review count surges.
    home3 = _menu(("Espresso", "Coffee", 500))
    prev_brew = _snap("b1", "Brew Lab", "F-7", [("Latte", "Coffee", 520, [])],
                      [("Google", 4.5, 100)], PREV)
    cur_brew = _snap("b1", "Brew Lab", "F-7", [("Latte", "Coffee", 520, [])],
                     [("Google", 4.6, 280)], NOW)
    scenarios.append(EvalScenario(
        name="review_momentum",
        description="A rival gaining reviews fast must read as rising and surface in headlines.",
        home_menu=home3,
        current=[cur_brew],
        previous={"b1": prev_brew},
        scope=Scope.LOCAL,
        expect=Expectation(
            rising_competitors=["Brew Lab"],
            headline_must_mention=["climbing"],
        ),
    ))

    # 4) Menu gap: multiple rivals sell a category we don't carry.
    home4 = _menu(("Espresso", "Coffee", 500), ("Latte", "Coffee", 550))
    g1 = _snap("g1", "Chai Khana", "F-6", [("Karak Chai", "Tea", 250, ["tea"])],
               [("Google", 4.3, 300)], NOW)
    g2 = _snap("g2", "Roastery 22", "F-10", [("Kashmiri Chai", "Tea", 350, ["tea"])],
               [("Google", 4.4, 320)], NOW)
    scenarios.append(EvalScenario(
        name="menu_gap_tea",
        description="Tea sold by 2+ rivals but absent from our menu must be flagged as a gap.",
        home_menu=home4,
        current=[g1, g2],
        previous={"g1": g1, "g2": g2},
        scope=Scope.LOCAL,
        expect=Expectation(
            menu_gap_categories=["tea"],
            headline_must_mention=["tea"],
        ),
    ))

    # 5) National trend: a tag recurs across cities.
    home5 = _menu(("Espresso", "Coffee", 500))
    n1 = _snap("n1", "Karachi Coffee Co", "DHA",
               [("Spanish Latte", "Coffee", 750, ["viral", "spanish-latte"])],
               [("Google", 4.4, 300)], NOW, city="Karachi")
    n2 = _snap("n2", "Lahore Loaf", "Gulberg",
               [("Spanish Latte", "Coffee", 720, ["viral", "spanish-latte"])],
               [("Google", 4.2, 280)], NOW, city="Lahore")
    scenarios.append(EvalScenario(
        name="national_trend_viral",
        description="A tag appearing across multiple cities must register as a national trend.",
        home_menu=home5,
        current=[n1, n2],
        previous={},
        scope=Scope.NATIONAL,
        expect=Expectation(
            national_trend_labels=["viral", "spanish-latte"],
            headline_must_mention=["viral"],
        ),
    ))

    return scenarios


SCENARIOS: list[EvalScenario] = _scenarios()
