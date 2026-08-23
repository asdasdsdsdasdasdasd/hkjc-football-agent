"""SQLite schema and checkpoint helpers for the HKJC scrape pipeline."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.config import DEFAULT_DB, MAX_RETRIES

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
  match_id TEXT PRIMARY KEY,
  match_date TEXT NOT NULL,
  front_end_id TEXT,
  api_id TEXT,
  competition TEXT,
  teams TEXT,
  ht_score TEXT,
  ft_score TEXT,
  status TEXT DEFAULT 'pending',
  retries INTEGER DEFAULT 0,
  last_error TEXT,
  scraped_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON matches(status);
CREATE INDEX IF NOT EXISTS idx_date ON matches(match_date);
CREATE TABLE IF NOT EXISTS day_locks (
  match_date TEXT PRIMARY KEY,
  worker_id TEXT NOT NULL,
  locked_at TEXT NOT NULL
);
"""


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    last_err: sqlite3.OperationalError | None = None
    for attempt in range(30):
        try:
            conn = sqlite3.connect(path, timeout=60)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=60000")
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if mode.lower() != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            return conn
        except sqlite3.OperationalError as exc:
            last_err = exc
            if "locked" in str(exc).lower() and attempt < 29:
                time.sleep(0.5 + attempt * 0.2)
                continue
            raise
    if last_err:
        raise last_err
    raise RuntimeError("connect failed")


def upsert_match(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO matches (
          match_id, match_date, front_end_id, api_id, competition, teams,
          ht_score, ft_score, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'pending'))
        ON CONFLICT(match_id) DO UPDATE SET
          match_date=excluded.match_date,
          front_end_id=COALESCE(excluded.front_end_id, matches.front_end_id),
          api_id=COALESCE(excluded.api_id, matches.api_id),
          competition=COALESCE(excluded.competition, matches.competition),
          teams=COALESCE(excluded.teams, matches.teams),
          ht_score=COALESCE(excluded.ht_score, matches.ht_score),
          ft_score=COALESCE(excluded.ft_score, matches.ft_score)
        WHERE matches.status != 'done'
        """,
        (
            row["match_id"],
            row["match_date"],
            row.get("front_end_id"),
            row.get("api_id"),
            row.get("competition"),
            row.get("teams"),
            row.get("ht_score"),
            row.get("ft_score"),
            row.get("status"),
        ),
    )
    conn.commit()


def mark_done(conn: sqlite3.Connection, match_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE matches
        SET status='done', scraped_at=?, last_error=NULL
        WHERE match_id=?
        """,
        (now, match_id),
    )
    conn.commit()


def reset_date_search_errors(conn: sqlite3.Connection) -> int:
    """Reset bulk-false errors from failed day searches back to pending."""
    cur = conn.execute(
        """
        UPDATE matches
        SET status='pending', retries=0, last_error=NULL
        WHERE status='error' AND last_error LIKE 'Date search failed%'
        """
    )
    conn.commit()
    return cur.rowcount


def mark_error(conn: sqlite3.Connection, match_id: str, error: str) -> None:
    conn.execute(
        """
        UPDATE matches
        SET status='error', retries=retries+1, last_error=?
        WHERE match_id=?
        """,
        (error[:2000], match_id),
    )
    conn.commit()


def update_scores(
    conn: sqlite3.Connection,
    match_id: str,
    ht_score: str | None,
    ft_score: str | None,
) -> None:
    conn.execute(
        """
        UPDATE matches SET ht_score=?, ft_score=? WHERE match_id=?
        """,
        (ht_score, ft_score, match_id),
    )
    conn.commit()


def fetch_pending(
    conn: sqlite3.Connection,
    *,
    match_id: str | None = None,
    match_date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    max_retries: int = MAX_RETRIES,
) -> list[sqlite3.Row]:
    clauses = ["(status='pending' OR (status='error' AND retries < ?))"]
    params: list[Any] = [max_retries]

    if match_id:
        clauses.append("match_id=?")
        params.append(match_id)
    if match_date:
        clauses.append("match_date=?")
        params.append(match_date)
    if start_date:
        clauses.append("match_date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("match_date <= ?")
        params.append(end_date)

    where = " AND ".join(clauses)
    cur = conn.execute(
        f"""
        SELECT * FROM matches
        WHERE {where}
        ORDER BY match_date, match_id
        """,
        params,
    )
    return cur.fetchall()


def status_summary(conn: sqlite3.Connection) -> dict[str, int]:
    cur = conn.execute(
        """
        SELECT status, COUNT(*) AS n FROM matches GROUP BY status
        """
    )
    return {row["status"]: row["n"] for row in cur.fetchall()}


def count_by_date(conn: sqlite3.Connection, start: str, end: str) -> int:
    cur = conn.execute(
        """
        SELECT COUNT(*) AS n FROM matches
        WHERE match_date >= ? AND match_date <= ?
        """,
        (start, end),
    )
    return int(cur.fetchone()["n"])


def clear_day_locks(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM day_locks")
    conn.commit()


def release_day_lock(conn: sqlite3.Connection, match_date: str) -> None:
    conn.execute("DELETE FROM day_locks WHERE match_date = ?", (match_date,))
    conn.commit()


def pending_day_count(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    max_retries: int = MAX_RETRIES,
) -> int:
    cur = conn.execute(
        """
        SELECT COUNT(DISTINCT match_date) AS n
        FROM matches
        WHERE match_date >= ? AND match_date <= ?
          AND (status='pending' OR (status='error' AND retries < ?))
        """,
        (start, end, max_retries),
    )
    return int(cur.fetchone()["n"])


def claim_next_pending_day(
    conn: sqlite3.Connection,
    worker_id: str,
    start: str,
    end: str,
    *,
    max_retries: int = MAX_RETRIES,
) -> str | None:
    """Atomically claim the next calendar day that still has pending work."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT m.match_date
            FROM matches m
            WHERE m.match_date >= ? AND m.match_date <= ?
              AND (m.status='pending' OR (m.status='error' AND m.retries < ?))
              AND NOT EXISTS (
                SELECT 1 FROM day_locks dl WHERE dl.match_date = m.match_date
              )
            ORDER BY m.match_date
            LIMIT 1
            """,
            (start, end, max_retries),
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None
        day = row["match_date"]
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO day_locks (match_date, worker_id, locked_at)
            VALUES (?, ?, ?)
            """,
            (day, worker_id, now),
        )
        conn.commit()
        return day
    except Exception:
        conn.execute("ROLLBACK")
        raise
