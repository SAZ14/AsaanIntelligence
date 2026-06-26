from __future__ import annotations
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from app.agents.scout.config import FRESHNESS_MINUTES, IG_POSTS_PER_PROFILE, enabled_sources
from app.core.db import SessionLocal, Run, Finding as DBFinding, Report
from app.agents.scout.schemas import FindingSchema
from app.agents.scout.cleaning import clean_findings
from app.agents.scout.analysis import enrich_findings, build_report
from app.agents.scout.discovery import confirm_seed_competitors, discover_new_competitors, get_all_competitors

logger = logging.getLogger(__name__)

RAW_DATA_DIR = Path("data/raw")
RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)


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
    """Run all enabled scrapers. Returns (findings, sources_ok, sources_failed)."""
    sources = enabled_sources()
    findings: list[FindingSchema] = []
    ok: list[str] = []
    failed: list[str] = []

    if sources["firecrawl"]:
        try:
            from app.agents.scout.scrapers.firecrawl_scraper import find_menu_and_offers
            fc_findings: list[FindingSchema] = []
            for comp in competitors:
                fc_findings.extend(find_menu_and_offers(comp))
            findings.extend(fc_findings)
            ok.append("firecrawl")
            logger.info("Firecrawl: %d findings", len(fc_findings))
        except Exception as exc:
            logger.error("Firecrawl scraper failed: %s", exc)
            failed.append("firecrawl")
    else:
        logger.info("Firecrawl skipped — no API key")

    if sources["instagram"]:
        try:
            from app.agents.scout.scrapers.instagram_scraper import fetch_recent_posts
            handles = [c["instagram_handle"] for c in competitors if c.get("instagram_handle")]
            if handles:
                ig_findings = fetch_recent_posts(handles, limit=IG_POSTS_PER_PROFILE)
                handle_to_name = {
                    c["instagram_handle"]: c["name"]
                    for c in competitors if c.get("instagram_handle")
                }
                for f in ig_findings:
                    f.competitor_name = handle_to_name.get(f.competitor_name, f.competitor_name)
                findings.extend(ig_findings)
                ok.append("instagram")
                logger.info("Instagram: %d findings", len(ig_findings))
            else:
                logger.info("Instagram: no handles resolved yet")
        except Exception as exc:
            logger.error("Instagram scraper failed: %s", exc)
            failed.append("instagram")
    else:
        logger.info("Instagram skipped — no APIFY_TOKEN")

    if sources["google_places"]:
        try:
            from app.agents.scout.scrapers.places_scraper import fetch_reviews_and_rating
            gp_findings: list[FindingSchema] = []
            for comp in competitors:
                gp_findings.extend(fetch_reviews_and_rating(comp))
            findings.extend(gp_findings)
            ok.append("google_places")
            logger.info("Google Places: %d findings", len(gp_findings))
        except Exception as exc:
            logger.error("Google Places scraper failed: %s", exc)
            failed.append("google_places")
    else:
        logger.info("Google Places skipped — no API key")

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


def _store_name(store_id: int) -> str:
    with SessionLocal() as db:
        from app.core.db import Store
        s = db.query(Store).filter(Store.id == store_id).first()
        return s.name if s else "Restaurant"


def run(command: str, store_id: int = 1, freshness_minutes: int = FRESHNESS_MINUTES,
        user_message: str | None = None) -> str:
    command = command.lower().strip()

    if command == "help":
        return build_report("help", [], _store_name(store_id), user_message=user_message)

    is_live = command == "scout"
    latest_run, db_findings = _get_latest_run(store_id)

    if not is_live and latest_run is not None:
        age = datetime.utcnow() - latest_run.finished_at
        if age < timedelta(minutes=freshness_minutes):
            logger.info("Reusing latest run #%d (age: %s)", latest_run.id, age)
            findings = _findings_from_db(db_findings)
            freshness_note = _build_freshness_note(latest_run, is_live=False)
            enriched = enrich_findings(findings)
            return build_report(command, enriched, freshness_note, user_message=user_message)
        else:
            logger.info("Latest run too old (%s), fetching fresh data", age)
            is_live = True

    # --- Live fetch ---
    try:
        confirm_seed_competitors(store_id)
        discover_new_competitors(store_id)
    except Exception as exc:
        logger.error("Discovery step failed: %s", exc)

    competitors = get_all_competitors(store_id)
    logger.info("Running pipeline for %d competitors (store_id=%d)", len(competitors), store_id)

    with SessionLocal() as db:
        db_run = Run(store_id=store_id, command=command, status="running")
        db.add(db_run)
        db.commit()
        db.refresh(db_run)
        run_id = db_run.id

    raw_findings, sources_ok, sources_failed = _fetch_all_sources(competitors)
    _dump_raw(run_id, "all_raw", [f.model_dump() for f in raw_findings])

    seen_hashes: set[str] = set()
    if latest_run:
        for dbf in db_findings:
            seen_hashes.add(dbf.content_hash)

    cleaned = clean_findings(raw_findings, seen_hashes=seen_hashes)
    logger.info("Clean: %d → %d findings after dedup/noise", len(raw_findings), len(cleaned))

    enriched = enrich_findings(cleaned)
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

    report_text = build_report(command, enriched, freshness_note, user_message=user_message)

    with SessionLocal() as db:
        db.add(Report(
            store_id=store_id,
            run_id=run_id,
            command=command,
            report_text=report_text,
        ))
        db.commit()

    return report_text

