"""A record of every scheduled step (setlist.fm, Pollstar, Gemini, backup, ...):
when it ran, whether it finished, and its counts. The dashboard reads it."""

import json
import sqlite3
from typing import Callable

from . import db, state


def _clean(stats) -> dict:
    if not isinstance(stats, dict):
        return {}
    return {k: v for k, v in stats.items() if not str(k).startswith("_")}


def record(conn: sqlite3.Connection, step: str, fn: Callable[[], dict]) -> dict:
    """Run fn() as `step` and record it. The row is written before fn starts, so a
    run that is killed shows up as never finished. Exceptions are recorded and re-raised."""
    db.begin_write(conn)
    rid = conn.execute("INSERT INTO runs (step, started_at) VALUES (?, ?)",
                       (step, state._iso(state._now()))).lastrowid
    conn.commit()
    try:
        stats = fn()
    except BaseException as e:
        if conn.in_transaction:
            conn.rollback()
        _finish(conn, rid, False, {"error": f"{type(e).__name__}: {e}"[:300]})
        raise
    _finish(conn, rid, True, _clean(stats))
    return stats


def _finish(conn: sqlite3.Connection, rid: int, ok: bool, stats: dict) -> None:
    db.begin_write(conn)
    conn.execute("UPDATE runs SET finished_at = ?, ok = ?, stats = ? WHERE id = ?",
                 (state._iso(state._now()), int(ok), json.dumps(stats, default=str), rid))
    conn.commit()


def recent(conn: sqlite3.Connection, limit: int = 60, step: str | None = None) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM runs" + (" WHERE step = ?" if step else "") + " ORDER BY started_at DESC, id DESC LIMIT ?",
        ((step, limit) if step else (limit,))).fetchall()
    return [{**dict(r), "stats": json.loads(r["stats"]) if r["stats"] else {}} for r in rows]
