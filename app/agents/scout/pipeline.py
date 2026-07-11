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
from app.agents.scout.analysis import enrich_findings, build_report, ReportGenerationFailed
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


def _all_content_hashes(store_id: int) -> set[str]:
    """Every content_hash ever stored for this store, across ALL scout runs
    -- not just the latest one. Used to seed dedup so the same unchanged
    competitor review/post doesn't get re-stored as a "new" finding on
    every scrape cycle. Confirmed live: seeding dedup from only the single
    latest run's findings (via _get_latest_run) left content unchanged
    since two-runs-ago looking "new" again each time, accumulating up to 6
    duplicate copies of the same review across the store's run history
    (371 duplicate content_hash groups found on audit). Mirrors
    review_sources/db.py's existing_content_hashes(), which reputation's
    own pipeline already does correctly for the same reason."""
    with SessionLocal() as db:
        rows = db.query(DBFinding.content_hash).filter(DBFinding.store_id == store_id).all()
        return {r[0] for r in rows}


def _get_latest_run(store_id: int) -> tuple[Run | None, list[DBFinding]]:
    """The "runs" table is shared with the reputation agent (its own review
    checks write command="whatsapp_check" rows to the exact same table).
    Without excluding that here, whenever a reputation check completes more
    recently than any scout run, this would pick up the reputation run as
    scout's own "latest run" and load ITS findings (the store's own
    reviews, tagged competitor_name=<store name>, update_type='review')
    into a scout report as if they were competitor intelligence -- a real
    data-integrity bug, confirmed live: a "competitors" query fed
    Anatummy's own 5-star reviews to the report LLM, which correctly (if
    confusingly) noted "these aren't competitor findings, they're internal
    reviews" rather than reporting on any actual competitor."""
    with SessionLocal() as db:
        run = (
            db.query(Run)
            .filter(
                Run.store_id == store_id,
                Run.status.in_(["ok", "partial"]),
                Run.command != "whatsapp_check",
            )
            .order_by(Run.finished_at.desc())
            .first()
        )
        if run is None:
            return None, []
        findings = db.query(DBFinding).filter(DBFinding.run_id == run.id).all()
        db.expunge_all()
        return run, findings


TARGETED_COMPETITOR_FINDINGS_LIMIT = 30


def _get_all_findings_for_competitor(store_id: int, competitor_name: str) -> list[DBFinding]:
    """Every finding on record for ONE competitor, across all runs, most
    recent first -- not the single latest run's already-report-capped
    subset (_select_report_findings in analysis.py deliberately caps each
    competitor to REPORT_FINDINGS_PER_COMPETITOR=4 to keep a many-
    competitor report balanced, which is exactly the wrong limit when a
    staff member asked about one specific competitor by name and wants
    everything known about them). Findings are deduped by content_hash at
    collection time (clean_findings' seen_hashes), so an old finding here
    isn't accumulated noise -- it's a real historical signal, just capped
    at TARGETED_COMPETITOR_FINDINGS_LIMIT so one very-tracked competitor
    can't blow out the report context unboundedly."""
    with SessionLocal() as db:
        findings = (
            db.query(DBFinding)
            .filter(
                DBFinding.store_id == store_id,
                DBFinding.competitor_name == competitor_name,
                DBFinding.update_type != "review",
            )
            .order_by(DBFinding.collected_at.desc(), DBFinding.id.desc())
            .limit(TARGETED_COMPETITOR_FINDINGS_LIMIT)
            .all()
        )
        db.expunge_all()
        return findings


def _maybe_target_competitor(
    store_id: int, user_message: str | None, findings: list[FindingSchema],
) -> list[FindingSchema]:
    """If user_message clearly asks about one specific tracked competitor,
    replace the normal (report-capped, spread-across-all-competitors)
    findings with that competitor's full history instead. Falls through
    to the original findings unchanged for general questions, when no
    user_message is present, or when nothing matches -- never errors."""
    if not user_message:
        return findings
    try:
        from app.agents.scout.discovery import get_all_competitors
        from app.agents.scout.analysis import classify_target_competitor
        names = [c["name"] for c in get_all_competitors(store_id)]
        target = classify_target_competitor(user_message, names)
        if not target:
            return findings
        db_findings = _get_all_findings_for_competitor(store_id, target)
        if not db_findings:
            return findings
        logger.info(
            "scout.pipeline: targeted_competitor store=%d competitor=%r findings=%d",
            store_id, target, len(db_findings),
        )
        return _findings_from_db(db_findings)
    except Exception as exc:
        logger.warning("scout.pipeline: _maybe_target_competitor failed: %s", exc)
        return findings


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


def _fetch_all_sources(competitors: list[dict]) -> tuple[list[FindingSchema], list[str], list[str], bool]:
    """Run all enabled scrapers in parallel. Returns (findings, sources_ok,
    sources_failed, quota_exceeded) -- quota_exceeded is True if any source
    failed specifically due to Apify's account-level usage limit (as
    opposed to a routine per-item failure), which callers use to avoid
    treating a total-outage run like a normal empty one (see run())."""
    sources = enabled_sources()

    def _web() -> list[FindingSchema]:
        from app.agents.scout.scrapers.web_scraper import find_menu_and_offers
        from app.core.apify_errors import ApifyQuotaExceeded
        results: list[FindingSchema] = []
        quota_exc: ApifyQuotaExceeded | None = None
        with ThreadPoolExecutor(max_workers=min(len(competitors), 6)) as ex:
            futures = {ex.submit(find_menu_and_offers, comp): comp["name"] for comp in competitors}
            for future in as_completed(futures):
                try:
                    results.extend(future.result())
                except ApifyQuotaExceeded as exc:
                    logger.error("web scraper failed for %s: %s", futures[future], exc)
                    quota_exc = quota_exc or exc
                except Exception as exc:
                    logger.error("web scraper failed for %s: %s", futures[future], exc)
        # Only escalate to a full-source failure if NOTHING came back at all
        # -- a quota error on one competitor's search alongside real results
        # from others is still a partially useful run.
        if quota_exc is not None and not results:
            raise quota_exc
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
    quota_exceeded = False

    if not task_map:
        return findings, ok, failed, quota_exceeded

    from app.core.apify_errors import is_quota_error

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
                if is_quota_error(exc):
                    quota_exceeded = True

    return findings, ok, failed, quota_exceeded


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


def _most_recent_report(store_id: int):
    """Most recent saved ScoutReport across the WHOLE scout command family
    (scout, competitors, alerts, pricing, ...) for this store -- not scoped
    to one exact command. Confirmed live this distinction matters: a
    same-command-only lookup can miss a recent good report saved under a
    different (but still legitimate, non-whatsapp_check) command and
    resurface a much older, possibly broken one from the requested
    command's own history instead."""
    from app.core.db import ScoutReport
    with SessionLocal() as db:
        return (
            db.query(ScoutReport)
            .join(Run, Run.id == ScoutReport.run_id)
            .filter(ScoutReport.store_id == store_id, Run.command != "whatsapp_check")
            .order_by(Run.finished_at.desc())
            .first()
        )


def answer_from_cache(store_id: int, user_message: str, history: list[dict] | None = None) -> str:
    """Answer a natural-language scout question using only what's already
    stored -- NEVER dispatches a live Apify scrape, no matter how stale the
    cache is. A live scrape is expensive (7-45 min, real Apify credits) and
    reserved for the explicit "scout" command and the scheduled cron; a
    free-form question like "what's Burger Lab been up to" should always
    answer instantly from cache instead. This also sidesteps a real bug a
    live natural-language run used to cause: run()'s live path saves a new
    Run + ScoutReport row under command="scout", so a targeted question's
    narrow, single-competitor answer became the most recent "scout" report
    -- overwriting what a plain "scout" request would serve next, even
    though it never actually re-scraped everything. Since this path never
    calls run()'s live branch, it never writes a Run/Report row at all, so
    there's nothing for a later plain "scout" to collide with.

    If the message names one specific tracked competitor, answers from
    that competitor's full history (_maybe_target_competitor); otherwise
    uses the latest run's findings, however old they are."""
    store_name, store_category = _store_info(store_id)
    latest_run, db_findings = _get_latest_run(store_id)
    try:
        if latest_run is None:
            return build_report("scout", [], "Data: no scan yet", user_message=user_message,
                                store_name=store_name, store_category=store_category)
        findings = _findings_from_db(db_findings)
        findings = _maybe_target_competitor(store_id, user_message, findings)
        freshness_note = _build_freshness_note(latest_run, is_live=False)
        enriched = enrich_findings(findings, store_name=store_name, store_category=store_category)
        return build_report("scout", enriched, freshness_note, user_message=user_message,
                            store_name=store_name, store_category=store_category, history=history)
    except ReportGenerationFailed:
        # Never persisted here (this path never writes a Run/Report row),
        # so there's nothing to protect -- just don't leak a raw
        # "Report generation failed: ..." string to the user.
        return "Couldn't summarize that right now (a temporary hiccup on our end). Please try again in a moment."


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
            findings = _maybe_target_competitor(store_id, user_message, findings)
            freshness_note = _build_freshness_note(latest_run, is_live=False)
            enriched = enrich_findings(findings, store_name=store_name, store_category=store_category)
            try:
                return build_report(command, enriched, freshness_note, user_message=user_message,
                                    store_name=store_name, store_category=store_category)
            except ReportGenerationFailed:
                # Nothing new was written on this path (pure cache read) --
                # just avoid leaking a raw failure string to the user.
                return f"{freshness_note}\n\nCouldn't summarize that right now (a temporary hiccup on our end). Please try again in a moment."
        else:
            logger.info("scout.pipeline: cache_stale age_min=%d — live fetch", int(age.total_seconds() / 60))
            is_live = True

    # --- Live fetch ---
    # Atomic lock (Redis SET NX -- app/core/cache.py, same connection as
    # rate limits/cooldowns/the job queue) is the authoritative "is a live
    # fetch already running for this store" answer, checked by every
    # caller (cron, gateway/main.py's two dispatch sites, internal.py's
    # _scout() fallback) via is_locked(scout_live_lock_key(store_id))
    # before ever reaching here. Acquiring it here too, right before any
    # work starts, closes what used to be a real race: discovery
    # (confirm_seed_competitors/discover_new_competitors/prune_stale_
    # competitors below) can take real time, and the Postgres "running"
    # row used to not get written until after all of that -- so two
    # callers whose freshness checks landed within that window (e.g. the
    # cron poll and a staff message arriving moments apart) could both
    # decide independently to proceed and both start a full live fetch.
    # SET NX is atomic: only one caller can ever hold this key at a time,
    # no matter how close in time two attempts are. TTL matches
    # RUN_IN_FLIGHT_MINUTES as a safety net (releases itself even if the
    # process crashes before the finally below runs); the finally
    # releases it immediately on the normal path so the next genuine
    # staleness cycle doesn't wait out the full TTL for no reason.
    from app.core import cache as _cache
    from app.agents.scout.config import RUN_IN_FLIGHT_MINUTES
    lock_key = scout_live_lock_key(store_id)
    if not _cache.try_lock(lock_key, ttl_seconds=RUN_IN_FLIGHT_MINUTES * 60):
        logger.info("scout.pipeline: lock_held store=%d — already running elsewhere", store_id)
        return "Scout is already running, your report will arrive in a few minutes. Please wait."

    try:
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

        raw_findings, sources_ok, sources_failed, quota_exceeded = _fetch_all_sources(competitors)
        _dump_raw(run_id, "all_raw", [f.model_dump() for f in raw_findings])
        logger.info(
            "scout.pipeline: sources_done store=%d ok=%s failed=%s raw_findings=%d",
            store_id, sources_ok, sources_failed, len(raw_findings),
        )

        seen_hashes = _all_content_hashes(store_id)

        cleaned = clean_findings(raw_findings, seen_hashes=seen_hashes)
        logger.info("scout.pipeline: dedup store=%d raw=%d cleaned=%d", store_id, len(raw_findings), len(cleaned))
        cleaned = cap_findings_per_competitor(cleaned, MAX_FINDINGS_PER_COMPETITOR)
        logger.info("scout.pipeline: capped store=%d cleaned_capped=%d", store_id, len(cleaned))

        enriched = enrich_findings(cleaned, store_name=store_name, store_category=store_category)
        _store_findings(run_id, store_id, enriched)

        status = "ok" if not sources_failed else ("partial" if sources_ok else "error")
        run_finished_at = datetime.utcnow()
        with SessionLocal() as db:
            db_run = db.query(Run).filter(Run.id == run_id).first()
            if db_run:
                db_run.finished_at = run_finished_at
                db_run.status = status
                db_run.sources_ok = sources_ok
                db_run.sources_failed = sources_failed
                db_run.finding_count = len(enriched)
                db.commit()
        from app.core import apify_guard
        if status in ("ok", "partial"):
            _mark_scout_run_fresh(store_id, run_finished_at, freshness_minutes)
            apify_guard.record_success()
        elif quota_exceeded:
            # Total failure specifically because Apify's account-level quota
            # is exhausted -- every source failed the same way, not routine
            # per-item noise. Do NOT save a new (empty) report over a
            # perfectly good old one; serve whatever was last cached instead
            # (confirmed live: this used to happen every ~1 min during an
            # outage, each cycle overwriting the previous report with "No
            # competitor signals found in this run").
            apify_guard.record_quota_failure("scout")
            logger.error(
                "scout.pipeline: apify_quota_exceeded store=%d -- keeping previous cached report",
                store_id,
            )
            prev = _most_recent_report(store_id)
            if prev:
                return (
                    "⚠️ Live scan failed (data provider usage limit reached) -- "
                    "showing the most recent report on file instead:\n\n" + prev.report_text
                )
            return (
                "⚠️ Live scan failed (data provider usage limit reached) and no "
                "previous report is available yet. Please try again later."
            )

        freshness_note = _build_freshness_note(None, is_live=True)
        if sources_failed:
            freshness_note += f" (partial, {', '.join(sources_failed)} failed)"

        # Storage/counting above uses the real freshly-scraped `enriched`
        # unconditionally -- only what feeds the report itself swaps to a
        # specific competitor's full history when the question asks for one.
        report_findings = _maybe_target_competitor(store_id, user_message, enriched)
        try:
            report_text = build_report(command, report_findings, freshness_note, user_message=user_message,
                                       store_name=store_name, store_category=store_category)
        except ReportGenerationFailed:
            # Scraping itself succeeded (we're past the quota_exceeded
            # branch above) -- only the final LLM summarization call
            # failed. Confirmed live: this used to be swallowed inside
            # build_report() and saved as a real ScoutReport row (a
            # "Report generation failed: ..." string with no way to tell
            # it apart from a genuine report later), and one such
            # placeholder got resurfaced days afterward as "the most
            # recent report on file" for a completely different failure.
            # Same treatment as the quota-outage case: never persist this,
            # serve the last genuinely good report instead.
            logger.error(
                "scout.pipeline: report_generation_failed store=%d -- keeping previous cached report",
                store_id,
            )
            prev = _most_recent_report(store_id)
            if prev:
                return (
                    "⚠️ Report summarization failed (temporary issue) -- "
                    "showing the most recent report on file instead:\n\n" + prev.report_text
                )
            return "⚠️ Report summarization failed and no previous report is available yet. Please try again later."

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
    finally:
        _cache.release_lock(lock_key)


def _scout_cache_key(store_id: int) -> str:
    return f"scout:last_run:{store_id}"


def scout_live_lock_key(store_id: int) -> str:
    """Not underscore-prefixed -- gateway/main.py (x2) and
    gateway/internal.py's _scout() also need this exact key to pre-screen
    with cache.is_locked() before ever calling run(), so their "already
    running" reply matches what run()'s own try_lock() would decide."""
    return f"scout:live_lock:{store_id}"


def _mark_scout_run_fresh(store_id: int, finished_at, freshness_minutes: int) -> None:
    from app.core import cache as _cache
    ttl = freshness_minutes * 60
    if ttl > 0:
        _cache.set(_scout_cache_key(store_id), {"finished_at": finished_at.isoformat()}, ttl=ttl)


def _is_scout_fresh(store_id: int, freshness_minutes: int = FRESHNESS_MINUTES) -> bool:
    """Redis-backed fast path (app/core/cache.py -- same connection already
    used for rate limits/cooldowns/the job queue): a pure existence check
    with no need for the actual cached findings, unlike run()'s own inline
    cache check (which needs the Finding rows from Postgres regardless of
    outcome -- for report-building on a hit, or dedup on a miss -- so it
    can't skip that query the same way; Redis wouldn't save anything
    there). Falls back to Postgres (source of truth) on a Redis miss."""
    from app.core import cache as _cache

    cached = _cache.get(_scout_cache_key(store_id))
    if cached is not None:
        try:
            finished_at = datetime.fromisoformat(cached["finished_at"])
            return finished_at >= datetime.utcnow() - timedelta(minutes=freshness_minutes)
        except Exception:
            pass  # malformed cache entry -- fall through to Postgres

    latest_run, _ = _get_latest_run(store_id)
    if latest_run is None or latest_run.finished_at is None:
        return False
    fresh = latest_run.finished_at >= datetime.utcnow() - timedelta(minutes=freshness_minutes)
    if fresh:
        _mark_scout_run_fresh(store_id, latest_run.finished_at, freshness_minutes)
    return fresh


# Bounded worker count for run_scout_all()'s cron loop -- kept low relative
# to the 24-vCPU host on purpose: this bounds concurrent Apify actor fan-out
# (the actual scaling constraint), not CPU/RAM.
_SCOUT_CRON_CONCURRENCY = 3


def run_scout_all() -> None:
    """Scheduled job (see scripts/run_server.py) -- keeps every store's
    scout cache warm so staff almost always land on a cached report
    instead of waiting 7-45 minutes for a live run.

    Runs frequently (see scripts/run_server.py's interval trigger) but only
    actually scrapes a store once its cache has truly gone stale, checked
    here via _is_scout_fresh() before ever calling run("scout", ...) --
    command="scout" itself always forces a live fetch unconditionally
    (is_live = command == "scout" in run()), so without this pre-check a
    fixed clock-time cron and a staff member's own recent "scout" command
    would drift out of sync: a manual run shortly before a scheduled cron
    slot would either get wastefully re-scraped again minutes later, or
    (worse, on a coarser schedule) the cache could sit stale for hours
    between cron slots with nothing noticing. Polling often and checking
    real staleness here instead syncs the next scrape to "last real scrape
    + FRESHNESS_MINUTES" regardless of who triggered that last scrape or
    what the clock says.

    Every other command (competitors/alerts/pricing/...) reads whatever
    run() most recently cached for the store regardless of which command
    produced it, so one fresh "scout" run is enough to warm all of them.

    Bounded concurrency (_SCOUT_CRON_CONCURRENCY stores at a time), not a
    single sequential loop -- a single run already takes 7-45 minutes
    (confirmed live) and fans out many Apify actors internally per store, so
    running every store at once would multiply that fan-out and risk Apify
    rate limits. A small worker pool instead of one-at-a-time lets the cache
    stay warm across many more stores within FRESHNESS_MINUTES while keeping
    concurrent Apify load bounded and low; each worker re-checks the circuit
    breaker before starting so a trip mid-cycle stops queued stores too, not
    just next cycle's top-level check.

    Also checks the shared Apify circuit breaker (app.core.apify_guard,
    tripped after 3 consecutive account-level quota failures, shared with
    reputation's cron) before touching any store -- confirmed live this
    matters: without it, a 1-minute cron interval means an Apify outage
    gets retried up to 1440 times a day, which risks Apify rate-limiting
    or banning the account outright."""
    from app.core.db import SessionLocal, Store
    from app.core import apify_guard

    if apify_guard.breaker_open():
        logger.warning("scout.cron: apify breaker open -- skipping all stores this cycle")
        return

    with SessionLocal() as db:
        store_ids = [s.id for s in db.query(Store).all()]

    def _run_one(store_id: int) -> None:
        if _is_scout_fresh(store_id):
            logger.info("scout.cron: store=%d already fresh, skipping", store_id)
            return
        if apify_guard.breaker_open():
            logger.warning("scout.cron: apify breaker tripped mid-cycle -- skipping store=%d", store_id)
            return
        try:
            run("scout", store_id=store_id)
            logger.info("scout.cron: store=%d completed", store_id)
        except Exception as exc:
            logger.error("scout.cron: store=%d failed: %s", store_id, exc)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=_SCOUT_CRON_CONCURRENCY) as pool:
        list(pool.map(_run_one, store_ids))

