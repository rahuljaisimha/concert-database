"""SQLite ledger: schema and connection.

Tables are STRICT and use portable types (ISO-8601 text dates, 0/1 integers,
JSON as text) so the database can later be moved to Postgres with pgloader
without surprises. See docs/ARCHITECTURE.md.
"""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1
# How long a writer waits for another process's write to finish (seconds).
# Separate crawler processes share the database; see "Concurrency" in
# docs/ARCHITECTURE.md.
BUSY_TIMEOUT = 60.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

-- Artists. id is the MusicBrainz ID when known, otherwise 'name:<name_norm>'
-- (an unresolved artist seen in a listing but not matched to the graph).
CREATE TABLE IF NOT EXISTS artists (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    mbid      TEXT,
    type      TEXT,                       -- person | group | NULL
    in_graph  INTEGER NOT NULL DEFAULT 0 CHECK (in_graph IN (0, 1)),
    degree    INTEGER NOT NULL DEFAULT 0  -- connections in the MusicSceneMap graph
) STRICT;
CREATE INDEX IF NOT EXISTS idx_artists_name_norm ON artists(name_norm);

CREATE TABLE IF NOT EXISTS venues (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    city      TEXT,
    city_norm TEXT NOT NULL DEFAULT '',
    state     TEXT,
    country   TEXT,
    lat       REAL,
    lon       REAL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_venues_name_city ON venues(name_norm, city_norm);

-- A source's own venue ID (e.g. setlist.fm venue id) → our venue.
CREATE TABLE IF NOT EXISTS venue_source_ids (
    source    TEXT NOT NULL,
    native_id TEXT NOT NULL,
    venue_id  INTEGER NOT NULL REFERENCES venues(id),
    PRIMARY KEY (source, native_id)
) STRICT;

-- One row per real-world show (date + venue).
CREATE TABLE IF NOT EXISTS shows (
    id          INTEGER PRIMARY KEY,
    date        TEXT NOT NULL,             -- YYYY-MM-DD (local date of the show)
    venue_id    INTEGER NOT NULL REFERENCES venues(id),
    status      TEXT NOT NULL DEFAULT 'scheduled'
                CHECK (status IN ('scheduled', 'played', 'cancelled', 'postponed')),
    is_festival INTEGER NOT NULL DEFAULT 0 CHECK (is_festival IN (0, 1)),
    final       INTEGER NOT NULL DEFAULT 0 CHECK (final IN (0, 1)),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_shows_venue_date ON shows(venue_id, date);
CREATE INDEX IF NOT EXISTS idx_shows_date ON shows(date);

-- What one source says about one artist at one show. Every fact keeps its
-- provenance; billings are derived from claims.
CREATE TABLE IF NOT EXISTS claims (
    id         INTEGER PRIMARY KEY,
    show_id    INTEGER NOT NULL REFERENCES shows(id),
    artist_id  TEXT NOT NULL REFERENCES artists(id),
    source     TEXT NOT NULL,              -- setlistfm | jsonld | manual | ...
    native_id  TEXT NOT NULL,              -- the source's id for this record
    url        TEXT,
    role       TEXT NOT NULL DEFAULT 'unknown'
               CHECK (role IN ('headliner', 'opener', 'unknown')),
    position   INTEGER,                    -- billing order from the source, 0 = top
    confidence REAL NOT NULL,              -- 0..1, how sure this source is about the role
    extra      TEXT,                       -- JSON: source-specific features
    seen_at    TEXT NOT NULL,
    UNIQUE (source, native_id, artist_id)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_claims_show ON claims(show_id);
CREATE INDEX IF NOT EXISTS idx_claims_artist ON claims(artist_id);

-- Resolved bill: one row per (show, artist), recomputed from claims.
CREATE TABLE IF NOT EXISTS billings (
    show_id    INTEGER NOT NULL REFERENCES shows(id),
    artist_id  TEXT NOT NULL REFERENCES artists(id),
    role       TEXT NOT NULL CHECK (role IN ('headliner', 'opener', 'unknown')),
    position   INTEGER,
    confidence REAL NOT NULL,
    n_sources  INTEGER NOT NULL,
    PRIMARY KEY (show_id, artist_id)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_billings_artist ON billings(artist_id);

-- What was checked and when, per unit of work (see concertdb/state.py).
CREATE TABLE IF NOT EXISTS crawl_state (
    source        TEXT NOT NULL,
    key           TEXT NOT NULL,
    priority      INTEGER NOT NULL DEFAULT 0,
    last_checked  TEXT,
    last_changed  TEXT,
    next_due      TEXT NOT NULL,
    interval_days REAL NOT NULL DEFAULT 1,
    cursor        TEXT,                    -- JSON, source-specific resume point
    etag          TEXT,
    last_modified TEXT,
    content_hash  TEXT,
    failures      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, key)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_crawl_due ON crawl_state(source, next_due);

-- Requests and money spent, per source per day (budgets and caps).
CREATE TABLE IF NOT EXISTS usage (
    day      TEXT NOT NULL,
    source   TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    usd      REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, source)
) STRICT;
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def begin_write(conn: sqlite3.Connection) -> None:
    """Take the write lock now, unless this connection already holds a transaction.

    Use it right after a network response, before reading what to write: with
    BEGIN IMMEDIATE the read-then-insert steps (find or create a venue, an
    artist, a show) can't interleave with another process doing the same, so
    two crawlers never create the same venue twice. Commit before the next
    network request or sleep, so the lock is never held while waiting.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
