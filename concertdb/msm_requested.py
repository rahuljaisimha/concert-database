"""User-requested data from MusicSceneMap, stored like our own crawls.

MusicSceneMap fetches concert data from outside sources because a user asked for
something, and keeps it in its Supabase table `user_requested_data` (source,
key, data, version, fetched_at, seq), one row per (source, key); `seq` moves
forward whenever a row is added or its version changes. Nothing in it is about
the user. Sources so far:

  setlistfm  key = setlist id         the setlist as setlist.fm returned it
                                      (MusicSceneMap's venue-search function)
  pollstar   key = Pollstar venue id  { venue, events }: the venue's id, name,
                                      city, state, location and its upcoming
                                      events cut down to what we read (fetched in
                                      visitors' browsers by Discover)

Each run reads the rows past the last `seq` it handled (meta
`msm_requested_last_seq`) through the same read-only login as the search
misses, and hands each to its source's ingest (INGEST), which uses that
source's crawl code, so the data is stored exactly as if we had fetched it,
without spending our requests:

  setlistfm  setlistfm.parse_setlist / is_known / ingest_setlist: source
             "setlistfm", the setlist id as native_id, the bill re-inferred;
             unchanged setlists are skipped
  pollstar   the venue linked like `pollstar-add` (a new one is created and
             queued for our own Pollstar crawl), its events through
             pollstar.parse_events / ingest_event with extra.via
             "musicscenemap": anyone can call MusicSceneMap's
             user-requested-data function, so the marker makes these claims
             easy to find and drop

A row from a source with no ingest here is skipped (counted); an unreadable
row is skipped too. A new source: an ingest function in INGEST.
"""

import sqlite3

from . import db, misses
from .sources import pollstar, setlistfm

ENV = misses.ENV
CURSOR_KEY = "msm_requested_last_seq"
FETCH_LIMIT = 5000          # rows per run
VIA = "musicscenemap"


def fetch_from_postgres(db_url: str, after_seq: int, limit: int = FETCH_LIMIT) -> list[tuple[int, str, dict]]:
    """(seq, source, data) of the rows past `after_seq`, oldest first."""
    import psycopg2   # optional dependency: pip install -e '.[misses]'
    with psycopg2.connect(db_url, connect_timeout=30) as pg, pg.cursor() as cur:
        cur.execute("SELECT seq, source, data FROM user_requested_data WHERE seq > %s ORDER BY seq LIMIT %s",
                    (after_seq, limit))
        return [(int(seq), source, data) for seq, source, data in cur.fetchall()]


def _setlist(conn: sqlite3.Connection, data: dict, stats: dict) -> None:
    p = setlistfm.parse_setlist(data)
    if setlistfm.is_known(conn, p):
        stats["setlists_unchanged"] += 1
        return
    setlistfm.ingest_setlist(conn, p)
    stats["setlists"] += 1


def _pollstar_venue(conn: sqlite3.Connection, v: dict) -> tuple[int, bool]:
    """Our venue for a Pollstar venue (id, name, city, state), linking it if new. (venue id, newly linked)."""
    row = conn.execute("SELECT venue_id FROM venue_source_ids WHERE source = ? AND native_id = ?",
                       (pollstar.SOURCE, str(v["id"]))).fetchone()
    if row:
        return row["venue_id"], False
    linked = {"id": str(v["id"]), "name": pollstar._text(v.get("name")) or "",
              "city": pollstar._text(v.get("city")), "state": pollstar._text(v.get("state"))}
    return pollstar._link(conn, linked), True


def _pollstar(conn: sqlite3.Connection, data: dict, stats: dict) -> None:
    venue = data.get("venue") or {}
    if not venue.get("id") or not venue.get("name"):
        raise ValueError("venue without id or name")
    events = pollstar.parse_events(data)
    venue_id, new = _pollstar_venue(conn, venue)
    stats["pollstar_new_venues"] += new
    for e in events:
        pollstar.ingest_event(conn, e, venue_id, via=VIA)
    stats["pollstar_venues"] += 1
    stats["pollstar_events"] += len(events)


INGEST = {"setlistfm": _setlist, "pollstar": _pollstar}


def _cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (CURSOR_KEY,)).fetchone()
    return int(row["value"]) if row else 0


def import_requested(conn: sqlite3.Connection, fetch, log=print) -> dict:
    """Store the rows fetch(after_seq) -> [(seq, source, data)] returns, each through its source's ingest."""
    after = _cursor(conn)
    rows = fetch(after)
    stats = {"rows": len(rows), "setlists": 0, "setlists_unchanged": 0, "pollstar_venues": 0,
             "pollstar_new_venues": 0, "pollstar_events": 0, "unknown_source": 0, "unreadable": 0}
    done_through = after
    db.begin_write(conn)
    for seq, source, data in rows:
        ingest = INGEST.get(source)
        if ingest is None:
            stats["unknown_source"] += 1
            log(f"  seq {seq}: no ingest for source {source!r}, skipped")
        else:
            try:
                ingest(conn, data, stats)
            except (AttributeError, KeyError, TypeError, ValueError) as e:
                stats["unreadable"] += 1
                log(f"  seq {seq} ({source}): unreadable ({type(e).__name__}: {e}), skipped")
        done_through = seq
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (CURSOR_KEY, str(done_through)))
    conn.commit()
    return stats


def configured() -> str | None:
    return misses.configured()
