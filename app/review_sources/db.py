from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data_webhook" / "reviews.db"


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


def save_reviews(reviews: list[dict]) -> int:
    if not reviews:
        return 0

    added = 0
    with _connect() as conn:
        for r in reviews:
            cur = conn.execute(
                """INSERT OR IGNORE INTO reviews
                   (source, text, rating, review_date, url, author, collected_at, hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["source"],
                    r["text"],
                    r.get("rating"),
                    r.get("review_date", ""),
                    r.get("url", ""),
                    r.get("author", ""),
                    r["collected_at"],
                    r["hash"],
                ),
            )
            added += cur.rowcount

        sources = ",".join(sorted({r["source"] for r in reviews}))
        conn.execute(
            "INSERT INTO runs (ran_at, reviews_added, sources) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), added, sources),
        )
        conn.commit()

    return added


def get_recent_reviews(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reviews ORDER BY collected_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
