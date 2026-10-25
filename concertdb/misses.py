"""Crawl the artists MusicSceneMap users look for and don't find.

MusicSceneMap logs a "search miss" (Supabase table `search_misses`: id, query,
source, created_at) when someone looks for an artist its graph doesn't have: a
MusicBrainz ID when they picked the artist from the dropdown's MusicBrainz
results, else the name they typed. Its graph is built from the artists with
concerts here, so the way to get a missing artist into it is to crawl that
artist's concerts.

Each run reads the misses logged since the last one (MSM_MISSES_DB_URL, a
read-only Postgres login that can only SELECT from search_misses), resolves each
to a MusicBrainz ID, and moves that artist to the front of the setlist.fm queue
(seed.USER_PRIORITY, the same as `concertdb prioritize`):

  - a query that is an MBID is used as is, if MusicBrainz knows it;
  - a name is looked up among every MusicBrainz artist's names and aliases
    (data/musicbrainz.sqlite, `concertdb mb-load`), so an old name finds the
    artist ("Thee Oh Sees" -> Osees). Exactly one artist with that name -> it;
    several -> skipped as ambiguous (a pick from the dropdown logs the MBID, so
    those come through); none -> skipped (a typo, or not on MusicBrainz).

At most MAX_PER_RUN new artists a run, to keep setlist.fm's daily requests for
the rest of the queue; misses past the cap wait for the next run (the position
in the table, meta `misses_last_id`, only moves past what was handled). Data
flows one way: nothing is written back to MusicSceneMap.
"""

import os
import re
import sqlite3
from collections import Counter

from . import db, ledger, seed, state
from .normalize import norm
from .sources import setlistfm

ENV = "MSM_MISSES_DB_URL"
CURSOR_KEY = "misses_last_id"
MAX_PER_RUN = 25
FETCH_LIMIT = 2000          # misses read per run (they are tiny)
MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def fetch_from_postgres(db_url: str, after_id: int, limit: int = FETCH_LIMIT) -> list[tuple[int, str, str]]:
    """(id, query, source) of the misses logged after `after_id`, oldest first."""
    import psycopg2   # optional dependency: pip install -e '.[misses]'
    with psycopg2.connect(db_url, connect_timeout=30) as pg, pg.cursor() as cur:
        cur.execute("SELECT id, query, source FROM search_misses WHERE id > %s ORDER BY id LIMIT %s",
                    (after_id, limit))
        return [(int(i), q or "", s or "") for i, q, s in cur.fetchall()]


def resolve(conn: sqlite3.Connection, query: str) -> tuple[str | None, str]:
    """(MusicBrainz ID or None, why): see the module docstring."""
    q = query.strip()
    if MBID.match(q.lower()):
        mbid = q.lower()
        if db.has_musicbrainz(conn) and not conn.execute(
                "SELECT 1 FROM mb.artists WHERE mbid = ?", (mbid,)).fetchone():
            return None, "unknown MBID"
        return mbid, "mbid"
    if not db.has_musicbrainz(conn):
        return None, "no MusicBrainz names (run `concertdb mb-load`)"
    candidates = ledger._mb_candidates(conn, norm(q))
    if len(candidates) == 1:
        return candidates[0], "name"
    return None, "ambiguous" if candidates else "not found"


def _cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (CURSOR_KEY,)).fetchone()
    return int(row["value"]) if row else 0


def prioritize_misses(conn: sqlite3.Connection, fetch, max_per_run: int = MAX_PER_RUN, log=print) -> dict:
    """Read new misses with fetch(after_id) -> [(id, query, source)], prioritize what resolves."""
    after = _cursor(conn)
    rows = fetch(after)
    stats = {"misses": len(rows), "prioritized": 0, "already_queued": 0, "ambiguous": 0, "not_found": 0,
             "deferred": 0}
    seen: dict[str, str | None] = {}     # query -> mbid, so repeats of a query cost one lookup
    done_through = after
    added: Counter = Counter()
    for mid, query, source in rows:
        key = query.strip().lower()
        if key not in seen:
            mbid, why = resolve(conn, query)
            seen[key] = mbid
            if mbid is None:
                stats["ambiguous" if why == "ambiguous" else "not_found"] += 1
                log(f"  {query!r} ({source}): {why}, skipped")
        mbid = seen[key]
        if mbid and mbid not in added:
            unit = state.get(conn, setlistfm.SOURCE, f"artist:{mbid}")
            if unit is not None and unit["priority"] >= seed.USER_PRIORITY:
                stats["already_queued"] += 1
            elif stats["prioritized"] >= max_per_run:
                stats["deferred"] = sum(1 for r in rows if r[0] > done_through)
                break
            else:
                db.begin_write(conn)
                aid = ledger.get_or_create_artist(conn, query if not MBID.match(key) else mbid, mbid)
                state.ensure(conn, setlistfm.SOURCE, f"artist:{aid}", priority=seed.USER_PRIORITY)
                conn.commit()
                stats["prioritized"] += 1
                name = conn.execute("SELECT name FROM artists WHERE id = ?", (aid,)).fetchone()["name"]
                log(f"  {query!r} ({source}) -> {name} ({aid}): prioritized")
            added[mbid] += 1
        done_through = mid
    db.begin_write(conn)
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (CURSOR_KEY, str(done_through)))
    conn.commit()
    return stats


def configured() -> str | None:
    return os.environ.get(ENV) or None
