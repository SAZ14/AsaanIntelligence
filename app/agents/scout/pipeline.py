from __future__ import annotations
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func

from app.agents.scout.config import (
    FRESHNESS_MINUTES, IG_POSTS_PER_PROFILE, enabled_sources,
    MAX_SCRAPED_COMPETITORS, MAX_FINDINGS_PER_COMPETITOR,
)
from app.core.db import SessionLocal, Run, Finding as DBFinding, Report
from app.agents.scout.schemas import FindingSchema
from app.agents.scout.cleaning import clean_findings, cap_findings_per_competitor
from app.agents.scout.analysis import enrich_findings, build_report
from app.agents.scout.discovery import (
    confirm_seed_competitors, discover_new_competitors, get_all_competitors,
    prune_stale_competitors,
)

logger = logging.getLogger(__name__)

RAW_DATA_DIR = Path("data/raw")
RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)


def _select_competitors_to_scrape(
    competitors: list[dict], store_id: int, max_total: int,
) -> list[dict]:
    """Cap the scrape list without ever dropping curated entries.

    get_all_competitors() only grows over time (discovery adds up to
    MAX_NEW_COMPETITORS per run, nothing removes -- prune_stale_competitors
    handles that separately, but on any given run there can still be more
    unproven "discovered" entries than we want to pay to scrape). primary/
    seed rows are always kept since a human deliberately chose them. If
    "discovered" entries push the total over max_total, keep the ones with
    the strongest historical track record (most Finding rows ever recorded
    against that name) -- proven signal beats an unproven recent guess.
    """
    curated = [c for c in competitors if c.get("source") in ("primary", "seed")]
    discovered = [c for c in competitors if c.get("source") not in ("primary", "seed")]

    remaining = max_total - len(curated)
    if remaining <= 0:
        return curated[:max_total]
    if len(discovered) <= remaining:
        return curated + discovered

    with SessionLocal() as db:
        counts = dict(
            db.query(DBFinding.competitor_name, func.count(DBFinding.id))
            .filter(DBFinding.store_id == store_id)
            .group_by(DBFinding.competitor_name)
            .all()
        )
    discovered.sort(key=lambda c: counts.get(c["name"], 0), reverse=True)
    dropped = len(discovered) - remaining
    if dropped:
        logger.info(
            "scout.pipeline: competitor_cap store=%d dropping %d lowest-history discovered entries",
            store_id, dropped,
        )
    return curated + discovered[:remaining]


def _dump_raw(run_id: int, source: str, data: object) -> None:
    path = RAW_DATA_DIR / f"{run_id}_{source}.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:
        logger.warning("Failed to dump raw data to %s: %s", path, exc)


def _get_latest_run(store_id: int) -> tuple[Run | None, list[DBFinding]]:
    with SessionLocal() as db:
        run = (
            db.query(Run)
            .filter(Run.store_id == store_id, Run.status.in_(["ok", "partial"]))
            .order_by(Run.finished_at.desc())
            .first()
        )
        if run is None:
            return None, []
        findings = db.query(DBFinding).filter(DBFinding.run_id == run.id).all()
        db.expunge_all()
        return run, findings


def _findings_from_db(db_findings: list[DBFinding]) -> list[FindingSchema]:
    results = []
    for f in db_findings:
        try:
            results.append(FindingSchema(
                competitor_name=f.competitor_name,
                source_platform=f.source_platform,
                update_type=f.update_type,
                content_text=f.content_text,
                rating=f.rating,
                post_date=f.post_date,
                source_url=f.source_url,
                image_url=f.image_url,
                engagement=f.engagement,
                ai_summary=f.ai_summary,
                relevance_score=f.relevance_score,
                content_hash=f.content_hash,
            ))
        except Exception as exc:
            logger.warning("Could not convert DB finding %d: %s", f.id, exc)
    return results


def _fetch_all_sources(competitors: list[dict]) -> tuple[list[FindingSchema], list[str], list[str]]:
    """Run all enabled scrapers in parallel. Returns (findings, sources_ok, sources_failed)."""
    sources = enabled_sources()

    def _web() -> list[FindingSchema]:
        from app.agents.scout.scrapers.web_scraper import find_menu_and_offers
        results: list[FindingSchema] = []
        with ThreadPoolExecutor(max_workers=min(len(competitors), 6)) as ex:
            futures = {ex.submit(find_menu_and_offers, comp): comp["name"] for comp in competitors}
            for future in as_completed(futures):
                try:
                    results.extend(future.result())
                except Exception as exc:
                    logger.error("web scraper failed for %s: %s", futures[future], exc)
        return results

    def _instagram() -> list[FindingSchema]:
        from app.agents.scout.scrapers.instagram_scraper import fetch_recent_posts
        handles = [c["instagram_handle"] for c in competitors if c.get("instagram_handle")]
        if not handles:
            logger.info("Instagram: no handles resolved yet")
            return []
        handle_to_name = {
            c["instagram_handle"]: c["name"]
            for c in competitors if c.get("instagram_handle")
        }
        ig_findings = fetch_recent_posts(handles, limit=IG_POSTS_PER_PROFILE)
        for f in ig_findings:
            f.competitor_name = handle_to_name.get(f.competitor_name, f.competitor_name)
        return ig_findings

    def _google_reviews() -> list[FindingSchema]:
        from app.agents.scout.scrapers.google_reviews_scraper import fetch_reviews
        results: list[FindingSchema] = []
        for comp in competitors:
            results.extend(fetch_reviews(comp))
        return results

    task_map: dict[str, object] = {}
    if sources["web"]:
        task_map["web"] = _web
    else:
        logger.info("Web scraper skipped — no APIFY_TOKEN")
    if sources["instagram"]:
        task_map["instagram"] = _instagram
    else:
        logger.info("Instagram skipped — no APIFY_TOKEN")
    if sources["google_reviews"]:
        task_map["google_reviews"] = _google_reviews
    else:
        logger.info("Google Maps Reviews skipped — no APIFY_TOKEN")

    findings: list[FindingSchema] = []
    ok: list[str] = []
    failed: list[str] = []

    if not task_map:
        return findings, ok, failed

    with ThreadPoolExecutor(max_workers=len(task_map)) as executor:
        future_to_name = {executor.submit(fn): name for name, fn in task_map.items()}
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                source_findings = future.result()
                findings.extend(source_findings)
                ok.append(name)
                logger.info("%s: %d findings", name, len(source_findings))
            except Exception as exc:
                logger.error("%s scraper failed: %s", name, exc)
                failed.append(name)

    return findings, ok, failed


def _build_freshness_note(run: Run | None, is_live: bool) -> str:
    if is_live or run is None:
        return "Data: live this run"
    delta = datetime.utcnow() - run.finished_at
    minutes = int(delta.total_seconds() / 60)
    if minutes < 60:
        return f"Data: last run, {minutes} minutes ago"
    hours = minutes // 60
    return f"Data: last run, {hours} hour{'s' if hours != 1 else ''} ago"


def _store_findings(run_id: int, store_id: int, findings: list[FindingSchema]) -> None:
    with SessionLocal() as db:
        for f in findings:
            db.add(DBFinding(
                store_id=store_id,
                run_id=run_id,
                competitor_name=f.competitor_name,
                source_platform=f.source_platform,
                update_type=f.update_type,
                content_text=f.content_text,
                rating=f.rating,
                post_date=f.post_date,
                source_url=f.source_url,
                image_url=f.image_url,
                engagement=f.engagement,
                ai_summary=f.ai_summary,
                relevance_score=f.relevance_score,
                content_hash=f.content_hash,
            ))
        db.commit()


def _store_info(store_id: int) -> tuple[str, str]:
    with SessionLocal() as db:
        from app.core.db import Store
        s = db.query(Store).filter(Store.id == store_id).first()
        name = s.name if s else "the restaurant"
        category = s.category if s else "food"
        return name, category


def run(command: str, store_id: int = 1, freshness_minutes: int = FRESHNESS_MINUTES,
        user_message: str | None = None) -> str:
    t0 = time.monotonic()
    command = command.lower().strip()
    store_name, store_category = _store_info(store_id)
    logger.info("scout.pipeline: run_start store=%d command=%s store_name=%s", store_id, command, store_name)

    if command == "help":
        return build_report("help", [], "", user_message=user_message,
                            store_name=store_name, store_category=store_category)

    is_live = command == "scout"
    latest_run, db_findings = _get_latest_run(store_id)

    if not is_live and latest_run is not None:
        age = datetime.utcnow() - latest_run.finished_at
        if age < timedelta(minutes=freshness_minutes):
            logger.info("scout.pipeline: cache_hit run_id=%d age_min=%d", latest_run.id, int(age.total_seconds() / 60))
            findings = _findings_from_db(db_findings)
            freshness_note = _build_freshness_note(latest_run, is_live=False)
            enriched = enrich_findings(findings, store_name=store_name, store_category=store_category)
            return build_report(command, enriched, freshness_note, user_message=user_message,
                                store_name=store_name, store_category=store_category)
        else:
            logger.info("scout.pipeline: cache_stale age_min=%d — live fetch", int(age.total_seconds() / 60))
            is_live = True

    # --- Live fetch ---
    try:
        confirm_seed_competitors(store_id)
        discover_new_competitors(store_id)
        pruned = prune_stale_competitors(store_id)
        if pruned:
            logger.info("scout.pipeline: pruned %d stale competitors store=%d", pruned, store_id)
    except Exception as exc:
        logger.error("scout.pipeline: discovery_failed store=%d error=%s", store_id, exc)

    competitors = get_all_competitors(store_id)
    competitors = _select_competitors_to_scrape(competitors, store_id, MAX_SCRAPED_COMPETITORS)
    logger.info("scout.pipeline: scraping store=%d competitors=%d", store_id, len(competitors))

    with SessionLocal() as db:
        db_run = Run(store_id=store_id, command=command, status="running")
        db.add(db_run)
        db.commit()
        db.refresh(db_run)
        run_id = db_run.id

    raw_findings, sources_ok, sources_failed = _fetch_all_sources(competitors)
    _dump_raw(run_id, "all_raw", [f.model_dump() for f in raw_findings])
    logger.info(
        "scout.pipeline: sources_done store=%d ok=%s failed=%s raw_findings=%d",
        store_id, sources_ok, sources_failed, len(raw_findings),
    )

    seen_hashes: set[str] = set()
    if latest_run:
        for dbf in db_findings:
            seen_hashes.add(dbf.content_hash)

    cleaned = clean_findings(raw_findings, seen_hashes=seen_hashes)
    logger.info("scout.pipeline: dedup store=%d raw=%d cleaned=%d", store_id, len(raw_findings), len(cleaned))
    cleaned = cap_findings_per_competitor(cleaned, MAX_FINDINGS_PER_COMPETITOR)
    logger.info("scout.pipeline: capped store=%d cleaned_capped=%d", store_id, len(cleaned))

    enriched = enrich_findings(cleaned, store_name=store_name, store_category=store_category)
    _store_findings(run_id, store_id, enriched)

    status = "ok" if not sources_failed else ("partial" if sources_ok else "error")
    with SessionLocal() as db:
        db_run = db.query(Run).filter(Run.id == run_id).first()
        if db_run:
            db_run.finished_at = datetime.utcnow()
            db_run.status = status
            db_run.sources_ok = sources_ok
            db_run.sources_failed = sources_failed
            db_run.finding_count = len(enriched)
            db.commit()

    freshness_note = _build_freshness_note(None, is_live=True)
    if sources_failed:
        freshness_note += f" (partial — {', '.join(sources_failed)} failed)"

    report_text = build_report(command, enriched, freshness_note, user_message=user_message,
                               store_name=store_name, store_category=store_category)

    with SessionLocal() as db:
        db.add(Report(
            store_id=store_id,
            run_id=run_id,
            command=command,
            report_text=report_text,
        ))
        db.commit()

    elapsed = int(time.monotonic() - t0)
    logger.info(
        "scout.pipeline: run_complete store=%d command=%s findings=%d status=%s duration=%ds",
        store_id, command, len(enriched), status, elapsed,
    )
    return report_text


def run_scout_all() -> None:
    """Scheduled job (see scripts/run_server.py) -- runs a live "scout"
    scrape for every store once daily, so it's already sitting in the
    FRESHNESS_MINUTES (24h) cache by the time a staff member sends any
    scout command that day, instead of them waiting 7-45 minutes for a
    live run. command="scout" always forces a live fetch (is_live = command
    == "scout" in run(), unconditionally) regardless of how fresh the
    existing cache is -- that's exactly what's wanted here. Every other
    command (competitors/alerts/pricing/...) reads whatever run() most
    recently cached for the store regardless of which command produced
    it, so this one daily "scout" run is enough to warm all of them.

    Sequential, not parallel -- a single run already takes 7-45 minutes
    (confirmed live) and fans out many Apify actors internally per store;
    running several stores' scrapes concurrently would multiply that
    fan-out and risk Apify rate limits. Fine at today's store count;
    worth revisiting if this needs to cover many stores."""
    from app.core.db import SessionLocal, Store

    with SessionLocal() as db:
        store_ids = [s.id for s in db.query(Store).all()]

    for store_id in store_ids:
        try:
            run("scout", store_id=store_id)
            logger.info("scout.cron: store=%d completed", store_id)
        except Exception as exc:
            logger.error("scout.cron: store=%d failed: %s", store_id, exc)

