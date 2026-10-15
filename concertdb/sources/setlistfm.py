"""setlist.fm: past shows by artist (MusicBrainz ID) and by venue.

API docs: https://api.setlist.fm/docs/1.0/index.html
Limits on a standard key: ~2 requests/second and ~1,440 requests/day.

Units of work in crawl_state:
  setlistfm / artist:<mbid>   every setlist for an artist in the graph
  setlistfm / venue:<id>      every setlist at a venue (finds acts not in the graph)

First visit pages through the full history (resuming across days if the daily
budget runs out). Later visits read newest-first and stop at the first page
with nothing new.
"""

import json
import sqlite3
from datetime import datetime

from .. import ledger, state
from ..http import Fetcher
from ..infer import SetlistFacts, infer_bill

SOURCE = "setlistfm"
BASE = "https://api.setlist.fm/rest/1.0"
DAILY_LIMIT = 1400          # leave headroom under the ~1,440/day key limit
MIN_INTERVAL = 0.6          # seconds between requests
RECHECK_MIN_DAYS = 14
RECHECK_MAX_DAYS = 180
NOT_FOUND_DAYS = 180


class RateLimited(Exception):
    pass


class Client:
    def __init__(self, fetcher: Fetcher, api_key: str):
        self.fetcher = fetcher
        self.api_key = api_key

    def page(self, kind: str, ident: str, page: int):
        path = {"artist": f"/artist/{ident}/setlists", "venue": f"/venue/{ident}/setlists"}[kind]
        return self.fetcher.get(
            SOURCE,
            BASE + path,
            params={"p": str(page)},
            headers={"Accept": "application/json", "x-api-key": self.api_key},
        )


# ---------------------------------------------------------------- parsing

def parse_date(s: str) -> str:
    """setlist.fm dates are dd-MM-yyyy."""
    return datetime.strptime(s, "%d-%m-%Y").date().isoformat()


def parse_setlist(sl: dict) -> dict:
    venue = sl.get("venue") or {}
    city = venue.get("city") or {}
    coords = city.get("coords") or {}
    songs = 0
    for s in (sl.get("sets") or {}).get("set") or []:
        songs += len(s.get("song") or [])
    artist = sl.get("artist") or {}
    return {
        "id": sl["id"],
        "last_updated": sl.get("lastUpdated"),
        "date": parse_date(sl["eventDate"]),
        "url": sl.get("url"),
        "artist_mbid": artist.get("mbid") or None,
        "artist_name": artist.get("name") or "",
        "venue_id": venue.get("id"),
        "venue_name": venue.get("name") or "",
        "city": city.get("name"),
        "state": city.get("state"),
        "country": (city.get("country") or {}).get("name"),
        "lat": coords.get("lat"),
        "lon": coords.get("long"),
        "tour": (sl.get("tour") or {}).get("name"),
        "info": sl.get("info"),
        "song_count": songs,
    }


# ---------------------------------------------------------------- ingest

def is_known(conn: sqlite3.Connection, p: dict) -> bool:
    """Already stored with the same lastUpdated → nothing new."""
    row = conn.execute(
        "SELECT extra FROM claims WHERE source = ? AND native_id = ? LIMIT 1", (SOURCE, p["id"])
    ).fetchone()
    if not row or not row["extra"]:
        return False
    return json.loads(row["extra"]).get("last_updated") == p["last_updated"]


def ingest_setlist(conn: sqlite3.Connection, p: dict) -> int:
    """Store one setlist as a claim and re-infer that night's bill. Returns show id."""
    venue_id = ledger.get_or_create_venue(
        conn, p["venue_name"], p["city"], p["state"], p["country"], p["lat"], p["lon"],
        source=SOURCE, native_id=p["venue_id"],
    )
    show_id = ledger.find_or_create_show(conn, p["date"], venue_id)
    artist_id = ledger.get_or_create_artist(conn, p["artist_name"], p["artist_mbid"])
    extra = {k: p[k] for k in ("song_count", "tour", "info", "last_updated")}
    ledger.add_claim(conn, show_id, artist_id, SOURCE, p["id"], url=p["url"], extra=extra, resolve=False)
    if p["venue_id"]:
        state.ensure(conn, SOURCE, f"venue:{p['venue_id']}", priority=0)
    reinfer_show(conn, show_id)
    return show_id


def reinfer_show(conn: sqlite3.Connection, show_id: int) -> None:
    rows = conn.execute(
        "SELECT c.native_id, c.artist_id, c.url, c.extra, a.name FROM claims c"
        " JOIN artists a ON a.id = c.artist_id WHERE c.show_id = ? AND c.source = ?",
        (show_id, SOURCE),
    ).fetchall()
    facts, by_artist = [], {}
    for r in rows:
        extra = json.loads(r["extra"] or "{}")
        facts.append(SetlistFacts(r["artist_id"], r["name"], extra.get("song_count", 0),
                                  extra.get("tour"), extra.get("info")))
        by_artist[r["artist_id"]] = r
    for artist_id, role in infer_bill(facts).items():
        r = by_artist[artist_id]
        extra = json.loads(r["extra"] or "{}")
        extra["reason"] = role.reason
        ledger.add_claim(conn, show_id, artist_id, SOURCE, r["native_id"], role=role.role,
                         confidence=role.confidence, url=r["url"], extra=extra, resolve=False)
    ledger.resolve_show(conn, show_id)


# ----------------------------------------------------------------- crawl

def crawl(conn: sqlite3.Connection, client: Client, max_requests: int, log=print) -> dict:
    """Spend up to max_requests (and never more than DAILY_LIMIT today)."""
    stats = {"requests": 0, "setlists": 0, "new": 0, "units": 0}

    def budget_left() -> bool:
        return stats["requests"] < max_requests and state.requests_today(conn, SOURCE) < DAILY_LIMIT

    while budget_left():
        rows = state.due(conn, SOURCE, limit=20)
        if not rows:
            break
        for row in rows:
            if not budget_left():
                break
            try:
                _crawl_unit(conn, client, row, stats, budget_left, log)
            except RateLimited:
                log("setlist.fm rate limit hit; stopping this run")
                conn.commit()
                return stats
            conn.commit()
    return stats


def _crawl_unit(conn, client, row, stats, budget_left, log) -> None:
    key = row["key"]
    kind, ident = key.split(":", 1)
    cursor = state.get_cursor(row)
    backfill = not cursor.get("backfill_done")
    page = cursor.get("next_page", 1) if backfill else 1
    any_new = False
    stats["units"] += 1

    while budget_left():
        resp = client.page(kind, ident, page)
        stats["requests"] += 1
        state.record_usage(conn, SOURCE)
        if resp.status == 404:
            log(f"  {key}: not on setlist.fm")
            state.postpone(conn, SOURCE, key, NOT_FOUND_DAYS)
            return
        if resp.status == 429:
            raise RateLimited()
        if resp.status != 200:
            log(f"  {key}: HTTP {resp.status}")
            state.mark_failed(conn, SOURCE, key)
            return

        data = resp.json()
        setlists = data.get("setlist") or []
        new_on_page = 0
        for sl in setlists:
            stats["setlists"] += 1
            try:
                p = parse_setlist(sl)
            except (KeyError, ValueError):
                continue
            if is_known(conn, p):
                continue
            ingest_setlist(conn, p)
            new_on_page += 1
        stats["new"] += new_on_page
        any_new = any_new or new_on_page > 0

        total = int(data.get("total", 0))
        per_page = int(data.get("itemsPerPage", 20)) or 20
        last_page = page * per_page >= total or not setlists

        if backfill:
            if last_page:
                state.save_cursor(conn, SOURCE, key, {"backfill_done": True})
                break
            page += 1
            state.save_cursor(conn, SOURCE, key, {"next_page": page})
            conn.commit()
        else:
            # Newest first: a page with nothing new means we've caught up.
            if last_page or new_on_page == 0:
                break
            page += 1
    else:
        # Budget ran out mid-unit; it stays due and resumes from the cursor.
        return

    nxt = state.mark_checked(conn, SOURCE, key, changed=any_new,
                             min_days=RECHECK_MIN_DAYS, max_days=RECHECK_MAX_DAYS)
    log(f"  {key}: {'backfilled' if backfill else 'checked'}, next {nxt.date()}")
