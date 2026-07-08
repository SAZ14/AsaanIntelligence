"""Review storage via SQLAlchemy — uses the shared Finding / ScoutRun tables.

The reputation agent stores reviews as Finding rows with update_type='review'.
ai_summary holds the JSON blob with sentiment, draft reply, correlation, and
workflow status (pending / posted / ignored).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from app.core.db import SessionLocal, Finding, ScoutRun

logger = logging.getLogger(__name__)


def _embed_text(text: str) -> str | None:
    """JSON-encoded embedding vector for one review's content_text, or
    None if the model isn't available (no sentence_transformers locally,
    a broken download) or the text is empty -- callers must treat a None
    content_embedding as "not semantically searchable", not an error."""
    if not text or not text.strip():
        return None
    from app.core.embeddings import embedding_model
    model = embedding_model()
    if model is None:
        return None
    try:
        return json.dumps(model.encode(text).tolist())
    except Exception as exc:
        logger.warning("_embed_text failed: %s", exc)
        return None


def save_run(store_id: int, command: str) -> int:
    with SessionLocal() as db:
        run = ScoutRun(store_id=store_id, command=command, status="running")
        db.add(run)
        db.commit()
        db.refresh(run)
        return run.id


def update_run(
    run_id: int,
    status: str,
    sources_ok: list[str],
    sources_failed: list[str],
    finding_count: int,
) -> None:
    with SessionLocal() as db:
        run = db.query(ScoutRun).filter(ScoutRun.id == run_id).first()
        if run:
            run.finished_at = datetime.utcnow()
            run.status = status
            run.sources_ok = sources_ok
            run.sources_failed = sources_failed
            run.finding_count = finding_count
            db.commit()


def existing_content_hashes(store_id: int, hashes: list[str]) -> set[str]:
    """Which of these content hashes are already stored for this store.
    Used to filter out already-seen reviews BEFORE spending an LLM call on
    classifying or drafting a reply for them -- both wasteful (the same
    review getting reclassified every run) and, before this was checked
    up front, actively harmful: save_review_finding used to overwrite an
    existing row's ai_summary on every re-scrape, which could reset an
    already-posted or already-ignored review's workflow status back to
    pending/auto_closed the next time the same review happened to be
    re-scraped."""
    if not hashes:
        return set()
    with SessionLocal() as db:
        rows = (
            db.query(Finding.content_hash)
            .filter(Finding.store_id == store_id, Finding.content_hash.in_(hashes))
            .all()
        )
        return {r[0] for r in rows}


def save_review_finding(
    store_id: int,
    run_id: int,
    store_name: str,
    review: dict[str, Any],
    ai_summary: dict[str, Any],
    relevance_score: int = 0,
) -> str:
    """Returns "new", "duplicate", or "error".

    A review whose content_hash already exists for this store is left
    completely untouched -- no field is overwritten, including
    ai_summary. It used to always overwrite ai_summary regardless, which
    meant a review already marked "posted" or "ignored" by staff could
    silently flip back to "pending"/"auto_closed" if the same review
    happened to come back in a later scrape (a real risk once reviews are
    no longer capped to the newest 50 per check -- the same older review
    can now legitimately reappear across runs)."""
    post_date: datetime | None = None
    raw_date = review.get("review_date")
    if raw_date:
        date_str = str(raw_date).strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}", date_str):
            try:
                post_date = datetime.fromisoformat(date_str[:10])
            except ValueError:
                pass

    content_hash = review.get("hash", "")

    try:
        with SessionLocal() as db:
            existing = (
                db.query(Finding.id)
                .filter(
                    Finding.store_id == store_id,
                    Finding.content_hash == content_hash,
                )
                .first()
            )
            if existing:
                return "duplicate"

            payload = dict(
                store_id=store_id,
                run_id=run_id,
                competitor_name=store_name,
                source_platform=review.get("source", "unknown"),
                update_type="review",
                content_text=review.get("text", ""),
                rating=review.get("rating"),
                post_date=post_date,
                source_url=review.get("url"),
                ai_summary=json.dumps(ai_summary),
                relevance_score=relevance_score,
                content_hash=content_hash,
                # Computed only for genuinely new reviews -- a duplicate is
                # discarded above before this point, so no point spending
                # an embedding call on text that's about to be thrown away.
                content_embedding=_embed_text(review.get("text", "")),
            )
            db.add(Finding(**payload))
            db.commit()
            return "new"
    except Exception as exc:
        logger.error("save_review_finding failed: %s", exc)
        return "error"


def get_pending_finding(store_id: int) -> dict[str, Any] | None:
    with SessionLocal() as db:
        findings = (
            db.query(Finding)
            .filter(Finding.store_id == store_id, Finding.update_type == "review")
            .order_by(Finding.id.desc())
            .limit(20)
            .all()
        )
        for f in findings:
            if not f.ai_summary:
                continue
            try:
                summary = (
                    json.loads(f.ai_summary)
                    if isinstance(f.ai_summary, str)
                    else f.ai_summary
                )
                if summary.get("status") == "pending":
                    return {
                        "id": f.id,
                        "store_id": f.store_id,
                        "rating": f.rating,
                        "content_text": f.content_text,
                        "source_platform": f.source_platform,
                        "post_date": str(f.post_date) if f.post_date else None,
                        "source_url": f.source_url,
                        "content_hash": f.content_hash,
                        "ai_summary": summary,
                    }
            except Exception:
                continue
    return None


def update_finding_summary(finding_id: int, ai_summary: dict[str, Any]) -> None:
    with SessionLocal() as db:
        f = db.query(Finding).filter(Finding.id == finding_id).first()
        if f:
            f.ai_summary = json.dumps(ai_summary)
            db.commit()


def list_reviews(
    store_id: int,
    sentiment: str | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 15,
) -> tuple[list[dict], int]:
    """Reviews matching sentiment (positive/negative/neutral/mixed) and/or
    status (pending/posted/ignored/auto_closed), most recently POSTED first
    (the review's own post_date, not when we happened to scrape/insert it --
    those can drift apart, e.g. an older review surfacing in a later scrape).
    NULLS LAST so reviews with no post_date (a handful, from sources that
    don't reliably provide one) fall to the end rather than the front.

    ai_summary is a plain Text column holding a JSON string (not a native
    JSON/JSONB column), so filtering happens in Python after fetching --
    there's no cross-DB-compatible way to filter on it in SQL directly
    (production is Postgres, tests run on SQLite). Returns (one page of
    matches starting at offset, total match count) so a caller can say
    "showing 11-20 of 47" and page through with NEXT rather than silently
    truncating or re-fetching from the start each time.
    """
    with SessionLocal() as db:
        findings = (
            db.query(Finding)
            .filter(Finding.store_id == store_id, Finding.update_type == "review")
            .order_by(Finding.post_date.desc().nulls_last(), Finding.id.desc())
            .all()
        )

    matches: list[dict] = []
    for f in findings:
        if not f.ai_summary:
            continue
        try:
            summary = json.loads(f.ai_summary) if isinstance(f.ai_summary, str) else f.ai_summary
        except Exception:
            continue
        if sentiment and summary.get("sentiment") != sentiment:
            continue
        if status and summary.get("status") != status:
            continue
        matches.append({
            "id": f.id,
            "rating": f.rating,
            "content_text": f.content_text,
            "source_platform": f.source_platform,
            "post_date": str(f.post_date) if f.post_date else None,
            "source_url": f.source_url,
            "content_hash": f.content_hash,
            "ai_summary": summary,
        })

    return matches[offset:offset + limit], len(matches)


def get_recent_reviews(store_id: int, limit: int = 50) -> list[dict]:
    with SessionLocal() as db:
        findings = (
            db.query(Finding)
            .filter(Finding.store_id == store_id, Finding.update_type == "review")
            .order_by(Finding.collected_at.desc(), Finding.id.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": f.id,
                "source": f.source_platform,
                "text": f.content_text,
                "rating": f.rating,
                "review_date": str(f.post_date) if f.post_date else None,
                "url": f.source_url,
                "collected_at": str(f.collected_at) if f.collected_at else None,
                "hash": f.content_hash,
            }
            for f in findings
        ]


def search_reviews_semantic(
    store_id: int, query: str, top_k: int = 15, min_similarity: float = 0.25,
    sentiment: str | None = None,
) -> list[dict]:
    """Reviews whose TEXT is semantically relevant to `query`, most
    relevant first -- not "most recent". Fixes a real gap in the old
    get_recent_reviews(limit=10)-only approach to free-form review chat:
    a store can accumulate hundreds of reviews (confirmed live: 300+),
    and a question like "has anyone complained about parking" needs to
    find that review regardless of how long ago it was posted, not just
    whatever happens to be in the newest 10.

    Cosine similarity computed in Python (util.cos_sim, same pattern as
    app/agents/customer/community/intent.py's menu-intent matching) over
    every review with a stored content_embedding, not a SQL-level vector
    query -- there's no pgvector column on this table (see Finding.
    content_embedding's comment in app/core/db.py), and at the scale
    reviews actually reach per store this is fast enough to just do in
    memory. Returns [] (not an error) if the embedding model or Redis is
    unavailable, or if the store has no embedded reviews yet (e.g. not
    yet backfilled) -- callers should treat this the same as "nothing
    relevant found", not a failure needing a fallback message of its own.

    `sentiment`, if given, filters to reviews whose already-classified
    ai_summary.sentiment matches -- computed once during the normal
    review check, not re-classified here. Fixes a real, confirmed-live
    gap: pure topical similarity matches SUBJECT, not polarity, so
    "complaints about the branch" surfaced praise ("Very nice amazing
    Branch") right alongside actual complaints, since both mention
    "branch". Callers detect whether a query has a sentiment lean (see
    reputation.py's _detect_sentiment_lean) and pass it through here.
    """
    if not query or not query.strip():
        return []
    from app.core.embeddings import embedding_model
    model = embedding_model()
    if model is None:
        return []

    with SessionLocal() as db:
        findings = (
            db.query(Finding)
            .filter(
                Finding.store_id == store_id,
                Finding.update_type == "review",
                Finding.content_embedding.isnot(None),
            )
            .all()
        )
        rows = []
        for f in findings:
            if sentiment:
                try:
                    summary = json.loads(f.ai_summary) if f.ai_summary else {}
                except Exception:
                    summary = {}
                if summary.get("sentiment") != sentiment:
                    continue
            rows.append({
                "id": f.id,
                "source": f.source_platform,
                "text": f.content_text,
                "rating": f.rating,
                "review_date": str(f.post_date) if f.post_date else None,
                "url": f.source_url,
                "collected_at": str(f.collected_at) if f.collected_at else None,
                "hash": f.content_hash,
                "embedding": f.content_embedding,
            })
    if not rows:
        return []

    try:
        from sentence_transformers import util
        import torch
        query_vec = model.encode(query, convert_to_tensor=True)
        review_vecs = torch.tensor([json.loads(r["embedding"]) for r in rows])
        sims = util.cos_sim(query_vec, review_vecs)[0].tolist()
    except Exception as exc:
        logger.warning("search_reviews_semantic: similarity computation failed: %s", exc)
        return []

    scored = [
        (sim, r) for sim, r in zip(sims, rows) if sim >= min_similarity
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)

    results = []
    for sim, r in scored[:top_k]:
        r = dict(r)
        r.pop("embedding")
        r["similarity"] = round(sim, 3)
        results.append(r)
    return results


def save_reviews(
    reviews: list[dict],
    store_id: int,
    run_id: int = 1,
    store_name: str = "",
) -> int:
    if not reviews:
        return 0
    return sum(
        1
        for r in reviews
        if save_review_finding(store_id, run_id, store_name, r, {"status": "pending"}) == "new"
    )
