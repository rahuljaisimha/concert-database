"""Crawl bookkeeping: what is due, adaptive recheck intervals, request budgets.

Every unit of work (a setlist.fm artist, a venue calendar URL, ...) has a
crawl_state row. A run only touches rows whose next_due has passed, so nothing
is refetched by accident. When a check finds nothing new the interval doubles
(up to a cap); when something changes it drops back to the minimum.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def ensure(conn: sqlite3.Connection, source: str, key: str, priority: int = 0) -> None:
    """Register a unit of work (due immediately) if it isn't known yet."""
    conn.execute(
        "INSERT INTO crawl_state (source, key, priority, next_due) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (source, key) DO UPDATE SET priority = MAX(priority, excluded.priority)",
        (source, key, priority, _iso(_now())),
    )


def due(conn: sqlite3.Connection, source: str, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM crawl_state WHERE source = ? AND next_due <= ?"
        " ORDER BY priority DESC, next_due ASC LIMIT ?",
        (source, _iso(_now()), limit),
    ).fetchall()


def get(conn: sqlite3.Connection, source: str, key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM crawl_state WHERE source = ? AND key = ?", (source, key)
    ).fetchone()


def get_cursor(row: sqlite3.Row | None) -> dict:
    if row is None or not row["cursor"]:
        return {}
    return json.loads(row["cursor"])


def save_cursor(conn: sqlite3.Connection, source: str, key: str, cursor: dict) -> None:
    conn.execute(
        "UPDATE crawl_state SET cursor = ? WHERE source = ? AND key = ?",
        (json.dumps(cursor, sort_keys=True), source, key),
    )


def mark_checked(
    conn: sqlite3.Connection,
    source: str,
    key: str,
    changed: bool,
    min_days: float = 1,
    max_days: float = 90,
    etag: str | None = None,
    last_modified: str | None = None,
    content_hash: str | None = None,
) -> datetime:
    """Record a successful check and schedule the next one. Returns next_due."""
    row = get(conn, source, key)
    prev = row["interval_days"] if row else min_days
    interval = min_days if changed else min(max_days, max(min_days, prev * 2))
    now = _now()
    nxt = now + timedelta(days=interval)
    conn.execute(
        "UPDATE crawl_state SET last_checked = ?, next_due = ?, interval_days = ?, failures = 0,"
        " last_changed = CASE WHEN ? THEN ? ELSE last_changed END,"
        " etag = COALESCE(?, etag), last_modified = COALESCE(?, last_modified),"
        " content_hash = COALESCE(?, content_hash)"
        " WHERE source = ? AND key = ?",
        (_iso(now), _iso(nxt), interval, 1 if changed else 0, _iso(now),
         etag, last_modified, content_hash, source, key),
    )
    return nxt


def postpone(conn: sqlite3.Connection, source: str, key: str, days: float) -> None:
    """Push a unit of work out without counting it as a check (e.g. not found)."""
    now = _now()
    conn.execute(
        "UPDATE crawl_state SET last_checked = ?, next_due = ?, interval_days = ?"
        " WHERE source = ? AND key = ?",
        (_iso(now), _iso(now + timedelta(days=days)), days, source, key),
    )


def mark_failed(conn: sqlite3.Connection, source: str, key: str) -> None:
    """Retry later with exponential backoff: 1h, 2h, 4h ... capped at 7 days."""
    row = get(conn, source, key)
    failures = (row["failures"] if row else 0) + 1
    hours = min(24 * 7, 2 ** (failures - 1))
    conn.execute(
        "UPDATE crawl_state SET failures = ?, next_due = ? WHERE source = ? AND key = ?",
        (failures, _iso(_now() + timedelta(hours=hours)), source, key),
    )


# ------------------------------------------------------------------ usage

def record_usage(conn: sqlite3.Connection, source: str, requests: int = 1, usd: float = 0.0) -> None:
    day = _now().date().isoformat()
    conn.execute(
        "INSERT INTO usage (day, source, requests, usd) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (day, source) DO UPDATE SET"
        " requests = requests + excluded.requests, usd = usd + excluded.usd",
        (day, source, requests, usd),
    )


def requests_today(conn: sqlite3.Connection, source: str) -> int:
    row = conn.execute(
        "SELECT requests FROM usage WHERE day = ? AND source = ?",
        (_now().date().isoformat(), source),
    ).fetchone()
    return row["requests"] if row else 0


def usd_this_month(conn: sqlite3.Connection) -> float:
    month = _now().strftime("%Y-%m")
    row = conn.execute(
        "SELECT COALESCE(SUM(usd), 0) AS usd FROM usage WHERE day LIKE ?", (f"{month}-%",)
    ).fetchone()
    return row["usd"]
