#!/usr/bin/env python3
"""Run the Competitive Intelligence agent across all three tiers.

Offline-first: with no Browserbase/Supabase/Anthropic credentials this runs
entirely on bundled fixtures and deterministic analysis. With credentials set
it transparently uses the live scrapers, store, and Claude phrasing.

To demonstrate change detection without a live history, this script seeds a
temporary store with the baseline fixture, then "scrapes" the current fixture
and diffs the two.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.loader import load_menu
from app.models.competitive import Scope
from app.scrape.fixture import FixtureScraper, load_snapshots_file
from app.storage.jsonstore import JsonFileStore
from app.agents.competitive_intel import run_competitive_agent

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
COMP = DATA / "competitive"


def _client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic

        return anthropic.Anthropic()
    except Exception:
        return None


def _rule(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _print_local(report) -> None:
    _rule(f"PRICING vs AREA AVERAGE  ({report.home_venue})")
    if report.pricing:
        for p in report.pricing:
            print(f"  [{p.recommendation.upper():13s}] {p.category:10s} "
                  f"ours PKR {p.home_avg_price:>7,.0f} | area PKR {p.area_avg_price:>7,.0f} "
                  f"({p.pct_diff:+.0%}, n={p.rival_sample})")
            print(f"                 {p.headline}")
    else:
        print("  Not enough rival pricing data per category.")

    _rule("NEW DISHES ON RIVAL MENUS")
    if report.new_dishes:
        for d in report.new_dishes:
            flag = "NEW VENUE" if d.is_new_venue else d.momentum
            tags = f" [{', '.join(d.tags)}]" if d.tags else ""
            print(f"  {d.competitor_name} ({d.area}) — {d.dish_name} "
                  f"({d.category}, PKR {d.price:,.0f}) [{flag}]{tags}")
    else:
        print("  No new dishes since last capture.")

    _rule("ACTIVE PROMOTIONS")
    if report.promotions:
        for pr in report.promotions:
            disc = f" -{pr.discount_pct:.0f}%" if pr.discount_pct else ""
            print(f"  {pr.competitor_name} ({pr.area}): {pr.title}{disc}")
            if pr.description:
                print(f"      {pr.description}")
    else:
        print("  No active promotions detected.")

    _rule("REVIEW MOMENTUM")
    for t in report.review_trends:
        print(f"  [{t.direction.upper():7s}] {t.competitor_name:18s} ({t.area:5s}) "
              f"{t.rating_now:.1f}* | {t.reviews_now:>5,} reviews ({t.reviews_delta:+d}, {t.growth_pct:+.0%})")

    _rule("MENU GAPS (rivals carry, you don't)")
    if report.menu_gaps:
        for g in report.menu_gaps:
            print(f"  {g.category} — {g.rival_count} rivals (e.g. {', '.join(g.example_dishes)})")
    else:
        print("  No clear category gaps.")


def _print_trends(report) -> None:
    _rule(f"NATIONAL TRENDS  (scope: {report.scope})")
    if report.national_trends:
        for tr in report.national_trends:
            print(f"  [{tr.kind:8s}] {tr.label:18s} across {tr.venue_count} venues "
                  f"(e.g. {', '.join(tr.example_dishes)})")
    else:
        print("  No cross-venue trends detected.")


def _print_headlines(report) -> None:
    _rule("OWNER HEADLINES")
    if report.headlines:
        for h in report.headlines:
            print(f"  • {h}")
    else:
        print("  (no headlines)")
    if report.advisory:
        _rule("INTERNATIONAL ADVISORY")
        for a in report.advisory:
            print(f"  • {a}")


def main() -> None:
    home_menu = load_menu(DATA / "menu.csv")
    client = _client()
    print(f"Home menu: {len(home_menu)} items | Claude phrasing: "
          f"{'ON' if client else 'OFF (deterministic fallback)'}")

    scraper = FixtureScraper(COMP / "snapshot_current.json")

    with tempfile.TemporaryDirectory() as tmp:
        store = JsonFileStore(tmp)
        # Seed the baseline so the agent has history to diff against.
        store.save(load_snapshots_file(COMP / "snapshot_prev.json"))

        for scope in (Scope.LOCAL, Scope.NATIONAL, Scope.INTERNATIONAL):
            print("\n\n" + "#" * 72)
            print(f"# TIER: {scope.value.upper()}")
            print("#" * 72)
            report = run_competitive_agent(
                home_menu, scope=scope, scraper=scraper, store=store,
                client=client, persist=False,
            )
            print(f"\nScanned {report.competitor_count} competitors.")
            if scope == Scope.LOCAL:
                _print_local(report)
            else:
                _print_trends(report)
            _print_headlines(report)


if __name__ == "__main__":
    main()
