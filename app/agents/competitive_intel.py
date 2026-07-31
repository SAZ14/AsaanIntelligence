"""Competitive Intelligence agent.

Your eyes on every other restaurant in the city. It captures what rivals are
doing — new dishes, pricing, promotions, review momentum — and tells you where
you're falling behind or where you've got room to move.

Pipeline:  scrape (Browserbase / fixtures)  →  store (Supabase / JSON)
           →  deterministic analysis  →  Claude turns findings into headlines.

Three scopes (tiers):
  LOCAL          — direct rivals in Islamabad sectors (head-to-head).
  NATIONAL       — what's trending across Pakistan to adopt early.
  INTERNATIONAL  — advisory on global trends worth importing.

The deterministic core stands on its own; Claude only adds phrasing, and the
whole thing degrades to templated text when no API key is available.
"""

from __future__ import annotations

from datetime import datetime

from app.analysis.competitive import (
    CompetitiveReport,
    active_promotions,
    area_pricing,
    detect_new_dishes,
    menu_gaps,
    momentum_map,
    national_trends,
    review_trends,
)
from app.models.canonical import MenuItem
from app.models.competitive import Scope
from app.scrape import ScrapeTarget, get_scraper
from app.storage import get_store

DEFAULT_HOME_VENUE = "Sugar Rush"

# Default crawl targets per scope. In production these come from a watchlist;
# the fixture scraper only uses the area/country fields to filter its universe.
DEFAULT_LOCAL_TARGETS = [
    ScrapeTarget(name="*", area="F-6", city="Islamabad"),
    ScrapeTarget(name="*", area="F-7", city="Islamabad"),
    ScrapeTarget(name="*", area="F-10", city="Islamabad"),
    ScrapeTarget(name="*", area="E-7", city="Islamabad"),
]
DEFAULT_NATIONAL_TARGETS = [ScrapeTarget(name="*", country="Pakistan", query="trending restaurant dishes Pakistan")]
DEFAULT_INTERNATIONAL_TARGETS = [ScrapeTarget(name="*", query="global food trends going viral")]


def _default_targets(scope: Scope) -> list[ScrapeTarget]:
    if scope == Scope.NATIONAL:
        return DEFAULT_NATIONAL_TARGETS
    if scope == Scope.INTERNATIONAL:
        return DEFAULT_INTERNATIONAL_TARGETS
    return DEFAULT_LOCAL_TARGETS


def run_competitive_agent(
    home_menu: dict[str, MenuItem],
    home_venue: str = DEFAULT_HOME_VENUE,
    scope: Scope = Scope.LOCAL,
    targets: list[ScrapeTarget] | None = None,
    scraper=None,
    store=None,
    client=None,
    persist: bool = True,
) -> CompetitiveReport:
    scraper = scraper or get_scraper()
    store = store or get_store()
    targets = targets if targets is not None else _default_targets(scope)

    run_at = datetime.now()
    current = scraper.scrape(targets, scope)

    previous = store.latest_before(scope, run_at)
    if persist:
        store.save(current)

    report = CompetitiveReport(
        scope=scope.value,
        home_venue=home_venue,
        competitor_count=len(current),
    )

    if scope == Scope.LOCAL:
        trends = review_trends(current, previous)
        report.pricing = area_pricing(home_menu, current)
        report.new_dishes = detect_new_dishes(current, previous, momentum_map(trends))
        report.promotions = active_promotions(current)
        report.review_trends = trends
        report.menu_gaps = menu_gaps(home_menu, current)
    elif scope == Scope.NATIONAL:
        report.national_trends = national_trends(current)
        report.menu_gaps = menu_gaps(home_menu, current)
    else:  # INTERNATIONAL
        report.national_trends = national_trends(current)

    report.headlines = _generate_headlines(report, client)
    if scope == Scope.INTERNATIONAL:
        report.advisory = _international_advisory(home_menu, report, client)

    return report


# ── Narrative layer (Claude, with deterministic fallback) ──

def _fallback_headlines(report: CompetitiveReport) -> list[str]:
    lines: list[str] = []

    # Pricing: surface the single biggest gap.
    for p in report.pricing:
        if p.recommendation in ("raise", "premium_risk"):
            lines.append(p.headline)
            break

    # New dishes at a brand-new or rising venue read as the real threat;
    # a fresh opening is the headline you most want to hear, so it wins.
    threat_dishes = sorted(
        (d for d in report.new_dishes if d.is_new_venue or d.momentum == "rising"),
        key=lambda d: (not d.is_new_venue, d.momentum != "rising"),
    )
    if threat_dishes:
        d = threat_dishes[0]
        where = f"in {d.area}" if d.area else ""
        verb = "just opened and is" if d.is_new_venue else "is gaining traction and"
        lines.append(
            f"{d.competitor_name} {where} {verb} pushing \"{d.dish_name}\" "
            f"({d.category}, PKR {d.price:,.0f}) — worth a look."
        )

    # Strongest review momentum.
    rising = [t for t in report.review_trends if t.direction == "rising"]
    if rising:
        t = rising[0]
        lines.append(
            f"{t.competitor_name} ({t.area}) is climbing fast — "
            f"+{t.reviews_delta} reviews ({t.growth_pct:.0%}) at {t.rating_now:.1f}★."
        )

    # Menu gap.
    if report.menu_gaps:
        g = report.menu_gaps[0]
        ex = ", ".join(g.example_dishes[:2])
        lines.append(
            f"{g.rival_count} rivals sell {g.category} ({ex}) — you don't carry it at all."
        )

    # National trends.
    for tr in report.national_trends[:1]:
        ex = ", ".join(tr.example_dishes[:2])
        lines.append(
            f"\"{tr.label}\" is showing up across {tr.venue_count} venues ({ex}) — a trend to watch."
        )

    return lines


def _generate_headlines(report: CompetitiveReport, client) -> list[str]:
    fallback = _fallback_headlines(report)
    if client is None:
        return fallback

    facts = _facts_block(report)
    if not facts.strip():
        return fallback

    prompt = (
        f"You advise the owner of {report.home_venue}, a café in Islamabad. "
        "Below are competitive findings. Write 3-5 short, punchy, owner-facing "
        "headlines (one sentence each, no bullets, no preamble). Be specific and "
        "concrete; name venues, dishes, sectors and numbers. Each line should tell "
        "the owner where they're falling behind or where they can move.\n\n"
        f"FINDINGS:\n{facts}"
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        lines = [ln.strip(" -•\t") for ln in text.splitlines() if ln.strip()]
        return lines or fallback
    except Exception:
        return fallback


def _international_advisory(home_menu: dict[str, MenuItem], report: CompetitiveReport, client) -> list[str]:
    home_cats = sorted({mi.category for mi in home_menu.values() if mi.category})
    if client is None:
        return [
            "Global advisory needs a Claude API key to generate suggestions; "
            f"based on your categories ({', '.join(home_cats[:5])}), watch international "
            "dessert and specialty-coffee trends for ideas to pilot."
        ]
    prompt = (
        f"You advise {report.home_venue}, a café in Islamabad selling: "
        f"{', '.join(home_cats)}. Suggest 3-4 international food/beverage trends "
        "currently popular abroad that this café could realistically pilot, with a "
        "one-line rationale each. Be concrete (name the trend and a sample item)."
    )
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        return [ln.strip(" -•\t") for ln in text.splitlines() if ln.strip()]
    except Exception:
        return ["International advisory generation failed; check API connectivity."]


def _facts_block(report: CompetitiveReport) -> str:
    parts: list[str] = []
    for p in report.pricing[:4]:
        parts.append(
            f"- Pricing/{p.category}: ours PKR {p.home_avg_price:,.0f} vs area "
            f"PKR {p.area_avg_price:,.0f} ({p.pct_diff:+.0%}, {p.recommendation})"
        )
    for d in report.new_dishes[:4]:
        flag = "NEW VENUE" if d.is_new_venue else d.momentum
        parts.append(
            f"- New dish: {d.competitor_name} ({d.area}) \"{d.dish_name}\" "
            f"{d.category} PKR {d.price:,.0f} [{flag}]"
        )
    for t in report.review_trends[:3]:
        parts.append(
            f"- Reviews: {t.competitor_name} ({t.area}) {t.direction}, "
            f"{t.rating_now:.1f}★, {t.reviews_delta:+d} reviews ({t.growth_pct:+.0%})"
        )
    for g in report.menu_gaps[:3]:
        parts.append(f"- Menu gap: {g.category} sold by {g.rival_count} rivals ({', '.join(g.example_dishes[:2])})")
    for tr in report.national_trends[:4]:
        parts.append(f"- Trend ({tr.kind}): {tr.label} across {tr.venue_count} venues ({', '.join(tr.example_dishes[:2])})")
    return "\n".join(parts)
