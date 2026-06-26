from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data_webhook" / "reviews.db"


def _supabase():
    try:
        from app.database import supabase
        return supabase
    except Exception:
        return None


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                source       TEXT    NOT NULL,
                text         TEXT    NOT NULL,
                rating       REAL,
                review_date  TEXT,
                url          TEXT,
                author       TEXT,
                collected_at TEXT    NOT NULL,
                hash         TEXT    UNIQUE NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at         TEXT    NOT NULL,
                reviews_added  INTEGER NOT NULL,
                sources        TEXT    NOT NULL
            )
        """)
        conn.commit()


def save_run(store_id: int, command: str) -> int:
    client = _supabase()
    now_str = datetime.now(timezone.utc).isoformat()
    if client is not None and os.environ.get("ASAAN_TEST_MODE") != "1":
        res = client.table("runs").insert({
            "store_id": store_id,
            "command": command,
            "started_at": now_str,
            "status": "running"
        }).execute()
        if res.data:
            return res.data[0]["id"]
        return 0
    else:
        init_db()
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs (ran_at, reviews_added, sources) VALUES (?, 0, '')",
                (now_str,)
            )
            conn.commit()
            return cur.lastrowid or 0


def update_run(
    run_id: int,
    status: str,
    sources_ok: list[str],
    sources_failed: list[str],
    finding_count: int
) -> None:
    client = _supabase()
    now_str = datetime.now(timezone.utc).isoformat()
    if client is not None and os.environ.get("ASAAN_TEST_MODE") != "1":
        client.table("runs").update({
            "status": status,
            "finished_at": now_str,
            "sources_ok": sources_ok,
            "sources_failed": sources_failed,
            "finding_count": finding_count
        }).eq("id", run_id).execute()
    else:
        with _connect() as conn:
            conn.execute(
                "UPDATE runs SET reviews_added = ?, sources = ? WHERE id = ?",
                (finding_count, ",".join(sources_ok), run_id)
            )
            conn.commit()


def save_review_finding(
    store_id: int,
    run_id: int,
    store_name: str,
    review: dict[str, Any],
    ai_summary: dict[str, Any],
    relevance_score: int
) -> bool:
    client = _supabase()
    if client is not None and os.environ.get("ASAAN_TEST_MODE") != "1":
        import json
        import re
        
        post_date = review.get("review_date")
        if post_date:
            post_date = str(post_date).strip()
            if not re.match(r"^\d{4}-\d{2}-\d{2}", post_date):
                post_date = None
        else:
            post_date = None

        try:
            client.table("findings").upsert({
                "store_id": store_id,
                "run_id": run_id,
                "competitor_name": store_name,
                "source_platform": review["source"],
                "update_type": "review",
                "content_text": review["text"],
                "rating": review.get("rating"),
                "post_date": post_date,
                "source_url": review.get("url"),
                "collected_at": review["collected_at"],
                "ai_summary": json.dumps(ai_summary),
                "relevance_score": relevance_score,
                "content_hash": review["hash"]
            }, on_conflict="content_hash").execute()
            return True
        except Exception:
            return False
    else:
        init_db()
        with _connect() as conn:
            try:
                conn.execute(
                    """INSERT INTO reviews
                       (source, text, rating, review_date, url, author, collected_at, hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        review["source"],
                        review["text"],
                        review.get("rating"),
                        review.get("review_date", ""),
                        review.get("url", ""),
                        review.get("author", ""),
                        review["collected_at"],
                        review["hash"],
                    ),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False


def get_pending_finding(store_id: int) -> dict[str, Any] | None:
    client = _supabase()
    if client is not None and os.environ.get("ASAAN_TEST_MODE") != "1":
        import json
        res = client.table("findings").select("*").eq("store_id", store_id).order("id", desc=True).limit(10).execute()
        if not res.data:
            return None
        for row in res.data:
            summary_raw = row.get("ai_summary")
            if not summary_raw:
                continue
            try:
                summary = json.loads(summary_raw) if isinstance(summary_raw, str) else summary_raw
                if summary.get("status") == "pending":
                    row["ai_summary"] = summary
                    return row
            except Exception:
                continue
        return None
    return None


def update_finding_summary(finding_id: int, ai_summary: dict[str, Any]) -> None:
    client = _supabase()
    if client is not None and os.environ.get("ASAAN_TEST_MODE") != "1":
        import json
        client.table("findings").update({
            "ai_summary": json.dumps(ai_summary)
        }).eq("id", finding_id).execute()


def save_reviews(reviews: list[dict]) -> int:
    if not reviews:
        return 0

    added = 0
    for r in reviews:
        if save_review_finding(1, 1, "Sugar Rush", r, {"status": "pending"}, 0):
            added += 1
    return added


def get_recent_reviews(limit: int = 50) -> list[dict]:
    client = _supabase()
    if client is not None and os.environ.get("ASAAN_TEST_MODE") != "1":
        res = client.table("findings").select("*").eq("update_type", "review").order("collected_at", desc=True).limit(limit).execute()
        reviews = []
        for row in res.data:
            reviews.append({
                "id": row.get("id"),
                "source": row.get("source_platform", "Unknown"),
                "text": row.get("content_text", ""),
                "rating": row.get("rating"),
                "review_date": row.get("post_date"),
                "url": row.get("source_url"),
                "author": row.get("competitor_name", "Anonymous"),
                "collected_at": row.get("collected_at"),
                "hash": row.get("content_hash", "")
            })
        return reviews
    else:
        init_db()
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM reviews ORDER BY collected_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
