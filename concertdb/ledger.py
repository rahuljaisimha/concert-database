"""Writing to the ledger: artists, venues, shows, claims, and resolved billings.

Sources never write billings directly. They add claims ("source S says artist A
played show X as role R with confidence C"), and resolve_show() derives the
billing from all claims for that show.
"""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

from .normalize import norm

# How much each source's word counts when sources disagree.
SOURCE_WEIGHTS = {
    "manual": 1.0,
    "jsonld": 0.9,
    "pollstar": 0.9,
    "setlistfm": 0.8,
    "wayback": 0.8,
    "llm": 0.6,
    "gemini": 0.6,
}
DEFAULT_WEIGHT = 0.5

# Bills with more acts than this are treated as festivals: being on the same
# bill there doesn't mean one act opened for another.
FESTIVAL_MIN_ACTS = 8


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- artists

def get_or_create_artist(conn: sqlite3.Connection, name: str, mbid: str | None = None) -> str:
    """Return the artist id for a name (and MBID if the source has one).

    With an MBID the match is exact. Without one, a name that matches exactly
    one artist in the graph resolves to it; otherwise an unresolved
    'name:<norm>' artist is used so the fact is still recorded.
    """
    n = norm(name)
    if mbid:
        row = conn.execute("SELECT id FROM artists WHERE id = ?", (mbid,)).fetchone()
        if row:
            return row["id"]
        conn.execute(
            "INSERT INTO artists (id, name, name_norm, mbid) VALUES (?, ?, ?, ?)",
            (mbid, name, n, mbid),
        )
        return mbid

    rows = conn.execute(
        "SELECT id FROM artists WHERE name_norm = ? AND in_graph = 1", (n,)
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]

    aid = f"name:{n}"
    conn.execute(
        "INSERT OR IGNORE INTO artists (id, name, name_norm) VALUES (?, ?, ?)",
        (aid, name, n),
    )
    return aid


# ----------------------------------------------------------------- venues

def get_or_create_venue(
    conn: sqlite3.Connection,
    name: str,
    city: str | None = None,
    state: str | None = None,
    country: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    source: str | None = None,
    native_id: str | None = None,
) -> int:
    """Match by the source's own venue id first, then by name + city."""
    if source and native_id:
        row = conn.execute(
            "SELECT venue_id FROM venue_source_ids WHERE source = ? AND native_id = ?",
            (source, native_id),
        ).fetchone()
        if row:
            return row["venue_id"]

    n, c = norm(name), norm(city)
    row = conn.execute(
        "SELECT id FROM venues WHERE name_norm = ? AND city_norm = ?", (n, c)
    ).fetchone()
    if row:
        vid = row["id"]
        if lat is not None:
            conn.execute(
                "UPDATE venues SET lat = COALESCE(lat, ?), lon = COALESCE(lon, ?) WHERE id = ?",
                (lat, lon, vid),
            )
    else:
        cur = conn.execute(
            "INSERT INTO venues (name, name_norm, city, city_norm, state, country, lat, lon)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, n, city, c, state, country, lat, lon),
        )
        vid = cur.lastrowid

    if source and native_id:
        conn.execute(
            "INSERT OR IGNORE INTO venue_source_ids (source, native_id, venue_id) VALUES (?, ?, ?)",
            (source, native_id, vid),
        )
    return vid


# ------------------------------------------------------------------ shows

# Sources whose native_id names a whole event (one bill), not one act's set.
# Two different events from one of these are never the same show.
EVENT_SOURCES = frozenset({"pollstar", "jsonld", "manual"})


def find_or_create_show(
    conn: sqlite3.Connection,
    show_date: str,
    venue_id: int,
    artist_ids=(),
    source: str | None = None,
    native_id: str | None = None,
    exclude_show: int | None = None,
) -> int:
    """The show at this venue on this night that a source's record belongs to.

    A record already stored (same source + native_id) keeps its show. Otherwise
    a show at the venue counts as the same one when nothing rules it out (see
    _other_event) and either
      - it is on the same date, or
      - it is a day apart AND shares an act: some sources date a show that
        runs past midnight to the next day, but a venue's shows on
        consecutive nights are different shows.
    A shared act is preferred over a same-date match. `exclude_show` skips one
    show (used when re-placing the records of a show being split).
    """
    artist_ids = set(artist_ids)
    if source and native_id:
        row = conn.execute(
            "SELECT c.show_id, s.date FROM claims c JOIN shows s ON s.id = c.show_id"
            " WHERE c.source = ? AND c.native_id = ? AND c.show_id IS NOT ?",
            (source, native_id, exclude_show),
        ).fetchone()
        if row and row["date"] == show_date:
            return row["show_id"]

    d = date.fromisoformat(show_date)
    lo, hi = (d - timedelta(days=1)).isoformat(), (d + timedelta(days=1)).isoformat()
    best = None
    for r in conn.execute(
        "SELECT id, date FROM shows WHERE venue_id = ? AND date BETWEEN ? AND ? AND id IS NOT ?"
        " ORDER BY id", (venue_id, lo, hi, exclude_show),
    ).fetchall():
        claims = conn.execute(
            "SELECT artist_id, source, native_id FROM claims WHERE show_id = ?", (r["id"],)
        ).fetchall()
        if _other_event(claims, artist_ids, source, native_id):
            continue
        shared = bool(artist_ids & {c["artist_id"] for c in claims})
        exact = r["date"] == show_date
        if not (shared or exact):
            continue
        if best is None or (shared, exact) > best[0]:
            best = ((shared, exact), r["id"])
    if best:
        return best[1]

    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO shows (date, venue_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (show_date, venue_id, "played" if d < date.today() else "scheduled", ts, ts),
    )
    return cur.lastrowid


def _other_event(claims, artist_ids: set, source: str | None, native_id: str | None) -> bool:
    """Does this show already hold a different record from the same source that
    can't be the same performance? For an event source, any other event; for a
    per-act source (setlist.fm), another set by one of the same acts (a
    residency: same band, same venue, next night)."""
    if not (source and native_id):
        return False
    for c in claims:
        if c["source"] == source and c["native_id"] != native_id:
            if source in EVENT_SOURCES or c["artist_id"] in artist_ids:
                return True
    return False


def drop_show_if_empty(conn: sqlite3.Connection, show_id: int) -> bool:
    if conn.execute("SELECT 1 FROM claims WHERE show_id = ? LIMIT 1", (show_id,)).fetchone():
        return False
    conn.execute("DELETE FROM billings WHERE show_id = ?", (show_id,))
    conn.execute("DELETE FROM shows WHERE id = ?", (show_id,))
    return True


def set_show_status(conn: sqlite3.Connection, show_id: int, status: str) -> None:
    conn.execute(
        "UPDATE shows SET status = ?, updated_at = ? WHERE id = ?",
        (status, now_iso(), show_id),
    )


# ----------------------------------------------------------------- claims

def add_claim(
    conn: sqlite3.Connection,
    show_id: int,
    artist_id: str,
    source: str,
    native_id: str,
    role: str = "unknown",
    confidence: float = 0.5,
    position: int | None = None,
    url: str | None = None,
    extra: dict | None = None,
    resolve: bool = True,
) -> bool:
    """Insert or update a claim. Returns True if anything changed."""
    extra_json = json.dumps(extra, sort_keys=True) if extra else None
    old = conn.execute(
        "SELECT show_id, role, position, confidence, extra FROM claims"
        " WHERE source = ? AND native_id = ? AND artist_id = ?",
        (source, native_id, artist_id),
    ).fetchone()
    if old and (old["show_id"], old["role"], old["position"], old["confidence"], old["extra"]) == (
        show_id, role, position, confidence, extra_json,
    ):
        return False
    conn.execute(
        "INSERT INTO claims (show_id, artist_id, source, native_id, url, role, position,"
        " confidence, extra, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (source, native_id, artist_id) DO UPDATE SET"
        " show_id = excluded.show_id, url = excluded.url, role = excluded.role,"
        " position = excluded.position, confidence = excluded.confidence,"
        " extra = excluded.extra, seen_at = excluded.seen_at",
        (show_id, artist_id, source, native_id, url, role, position, confidence, extra_json, now_iso()),
    )
    if resolve:
        resolve_show(conn, show_id)
    if old and old["show_id"] != show_id:
        # The record moved (e.g. a rescheduled event): fix up the show it left.
        if not drop_show_if_empty(conn, old["show_id"]):
            resolve_show(conn, old["show_id"])
    return True


def _combine(confs: list[float]) -> float:
    """Independent evidence: 1 - Π(1 - c)."""
    p = 1.0
    for c in confs:
        p *= 1.0 - c
    return 1.0 - p


def resolve_show(conn: sqlite3.Connection, show_id: int) -> None:
    """Recompute billings for one show from its claims."""
    claims = conn.execute(
        "SELECT artist_id, source, role, position, confidence FROM claims WHERE show_id = ?",
        (show_id,),
    ).fetchall()
    by_artist: dict[str, list[sqlite3.Row]] = {}
    for c in claims:
        by_artist.setdefault(c["artist_id"], []).append(c)

    conn.execute("DELETE FROM billings WHERE show_id = ?", (show_id,))
    for artist_id, cs in by_artist.items():
        votes: dict[str, list[float]] = {"headliner": [], "opener": []}
        for c in cs:
            if c["role"] in votes:
                w = SOURCE_WEIGHTS.get(c["source"], DEFAULT_WEIGHT)
                votes[c["role"]].append(c["confidence"] * w)
        h, o = _combine(votes["headliner"]), _combine(votes["opener"])
        if h == 0 and o == 0:
            role, conf = "unknown", 0.0
        elif h >= o:
            role, conf = "headliner", h * (1 - o)
        else:
            role, conf = "opener", o * (1 - h)
        # Position from the most trusted source that gives one.
        positioned = [c for c in cs if c["position"] is not None]
        positioned.sort(key=lambda c: -SOURCE_WEIGHTS.get(c["source"], DEFAULT_WEIGHT))
        position = positioned[0]["position"] if positioned else None
        n_sources = len({c["source"] for c in cs})
        conn.execute(
            "INSERT INTO billings (show_id, artist_id, role, position, confidence, n_sources)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (show_id, artist_id, role, position, round(conf, 4), n_sources),
        )

    conn.execute(
        "UPDATE shows SET is_festival = ?, updated_at = ? WHERE id = ?",
        (1 if len(by_artist) >= FESTIVAL_MIN_ACTS else 0, now_iso(), show_id),
    )
