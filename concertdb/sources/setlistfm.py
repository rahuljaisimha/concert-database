"""setlist.fm: past shows by artist (MusicBrainz ID) and by venue.

API docs: https://api.setlist.fm/docs/1.0/index.html
Limits on a standard key: ~2 requests/second and ~1,440 requests/day.

Units of work in crawl_state:
  setlistfm / artist:<mbid>   every setlist for an artist in the graph
  setlistfm / venue:<id>      every setlist at a venue (finds acts not in the graph)

First visit pages through the full history (resuming across days if the daily
budget runs out). Later visits read newest-first and stop at the first page
with nothing new.

Nothing is held open while waiting on the network: the transaction is
committed before every request and every backoff sleep, so other processes
(the Pollstar crawler) can write in between. See db.begin_write.
"""

import json
import sqlite3
import time
from datetime import datetime

import requests

from .. import db, ledger, state
from ..http import Fetcher
from ..infer import SetlistFacts, infer_bill

SOURCE = "setlistfm"
BASE = "https://api.setlist.fm/rest/1.0"
DAILY_LIMIT = 1400          # leave headroom under the ~1,440/day key limit
MIN_INTERVAL = 1.0          # seconds between requests (start to start)
MAX_INTERVAL = 5.0
# On HTTP 429: wait, slow down for the rest of the run, retry the same page.
# After this many 429s in a row it's probably the daily limit: stop the run.
BACKOFF_SECONDS = (10, 30, 60, 120)
# On a timeout or dropped connection: wait, retry the same page. After this many
# in a row setlist.fm is down (or our network is): end the run cleanly, keeping
# what it stored; the unit resumes at the same page next time.
NETWORK_BACKOFF_SECONDS = (15, 60, 180)
sleep = time.sleep          # replaced in tests
# First pass reads only an artist's newest FIRST_PASS_PAGES pages (20 setlists
# each), so many artists get coverage quickly. The rest of their history is
# finished later in a deep pass at DEEP_PRIORITY, after every first pass.
# Artists you prioritized (USER_PRIORITY) always get their full history at once.
FIRST_PASS_PAGES = 10
DEEP_PRIORITY = 0
NO_CAP_PRIORITY = 1_000_000
RECHECK_MIN_DAYS = 14
RECHECK_MAX_DAYS = 180
NOT_FOUND_DAYS = 180


class RateLimited(Exception):
    pass


class NetworkDown(Exception):
    pass


class Client:
    def __init__(self, fetcher: Fetcher, api_key: str):
        self.fetcher = fetcher
        self.api_key = api_key

    def slow_down(self) -> float:
        self.fetcher.min_interval = min(MAX_INTERVAL, self.fetcher.min_interval * 1.5)
        return self.fetcher.min_interval

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
    artist_id = ledger.get_or_create_artist(conn, p["artist_name"], p["artist_mbid"])
    show_id = ledger.find_or_create_show(conn, p["date"], venue_id, [artist_id],
                                         source=SOURCE, native_id=p["id"])
    extra = {k: p[k] for k in ("song_count", "tour", "info", "last_updated", "date")}
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

def record_total(conn: sqlite3.Connection, key: str, total: int) -> None:
    """setlist.fm's own count of a unit's setlists (the dashboard's coverage denominator)."""
    conn.execute("INSERT INTO setlistfm_totals (key, total, seen_at) VALUES (?, ?, ?)"
                 " ON CONFLICT (key) DO UPDATE SET total = excluded.total, seen_at = excluded.seen_at",
                 (key, total, state._iso(state._now())))


def crawl(conn: sqlite3.Connection, client: Client, max_requests: int, log=print) -> dict:
    """Spend up to max_requests (and never more than DAILY_LIMIT today)."""
    stats = {"requests": 0, "setlists": 0, "new": 0, "units": 0, "rate_limited": 0, "network_errors": 0,
             "_streak": 0, "_net_streak": 0}

    def budget_left() -> bool:
        return stats["requests"] < max_requests and state.requests_today(conn, SOURCE) < DAILY_LIMIT

    try:
        while budget_left():
            rows = state.due(conn, SOURCE, limit=20)
            if not rows:
                break
            for row in rows:
                if not budget_left():
                    break
                _crawl_unit(conn, client, row, stats, budget_left, log)
                conn.commit()
    except RateLimited:
        log(f"setlist.fm: {len(BACKOFF_SECONDS) + 1} rate-limit responses in a row"
            " (probably the daily limit); stopping this run")
        conn.commit()
    except NetworkDown as e:
        log(f"setlist.fm: {len(NETWORK_BACKOFF_SECONDS) + 1} network errors in a row ({e}); stopping this run")
        stats["stopped"] = f"setlist.fm not answering ({e})"
        conn.commit()
    stats.pop("_streak", None)
    stats.pop("_net_streak", None)
    return stats


def _crawl_unit(conn, client, row, stats, budget_left, log) -> None:
    key = row["key"]
    kind, ident = key.split(":", 1)
    cursor = state.get_cursor(row)
    backfill = not cursor.get("backfill_done")
    deep = bool(cursor.get("deep"))
    capped = backfill and not deep and row["priority"] < NO_CAP_PRIORITY
    # A recheck that stopped part-way (budget, rate limit, crash) resumes at the
    # page it reached: the pages before it are stored, so starting again from
    # page 1 would find nothing new there and stop before reaching the rest.
    page = cursor.get("next_page", 1) if backfill else cursor.get("recheck_page", 1)
    any_new = not backfill and page > 1   # resumed: an earlier page had new setlists
    # The oldest show date reached on this unit's own pages (newest first, so everything since
    # then is fetched): the dashboard's "last 5 years fetched". Carried in every cursor saved.
    reached = cursor.get("reached")
    keep = lambda c: {**c, **({"reached": reached} if reached else {})}   # noqa: E731
    stats["units"] += 1

    while budget_left():
        if capped and page > FIRST_PASS_PAGES:
            state.defer_deep_pass(conn, SOURCE, key, keep({"deep": True, "next_page": page}), DEEP_PRIORITY)
            stats["deferred"] = stats.get("deferred", 0) + 1
            log(f"  {key}: newest {FIRST_PASS_PAGES} pages done; older history queued for the deep pass")
            return
        conn.commit()  # never hold the write lock across a request
        try:
            resp = client.page(kind, ident, page)
        except requests.RequestException as e:      # timeout, dropped connection
            db.begin_write(conn)
            stats["requests"] += 1
            stats["network_errors"] += 1
            state.record_usage(conn, SOURCE)        # it may have counted against the key
            conn.commit()
            streak = stats["_net_streak"]
            why = type(e).__name__
            if streak >= len(NETWORK_BACKOFF_SECONDS):
                raise NetworkDown(why) from e
            stats["_net_streak"] = streak + 1
            log(f"  {key}: {why}; waiting {NETWORK_BACKOFF_SECONDS[streak]}s, then page {page} again")
            sleep(NETWORK_BACKOFF_SECONDS[streak])
            continue
        stats["_net_streak"] = 0
        db.begin_write(conn)
        stats["requests"] += 1
        state.record_usage(conn, SOURCE)
        if resp.status == 404:
            log(f"  {key}: not on setlist.fm")
            record_total(conn, key, 0)
            state.postpone(conn, SOURCE, key, NOT_FOUND_DAYS)
            return
        if resp.status == 429:
            streak = stats["_streak"]
            stats["rate_limited"] += 1
            if streak >= len(BACKOFF_SECONDS):
                raise RateLimited()
            wait = max(resp.retry_after or 0, BACKOFF_SECONDS[streak])
            interval = client.slow_down() if hasattr(client, "slow_down") else None
            log(f"  {key}: HTTP 429 {resp.text[:200]!r}; waiting {wait:.0f}s"
                + (f", then {interval:.1f}s between requests" if interval else ""))
            stats["_streak"] = streak + 1
            conn.commit()
            sleep(wait)
            continue  # same page again
        stats["_streak"] = 0
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
            if backfill and (reached is None or p["date"] < reached):
                reached = p["date"]
            if is_known(conn, p):
                continue
            ingest_setlist(conn, p)
            new_on_page += 1
        stats["new"] += new_on_page
        any_new = any_new or new_on_page > 0

        total = int(data.get("total", 0))
        record_total(conn, key, total)
        per_page = int(data.get("itemsPerPage", 20)) or 20
        last_page = page * per_page >= total or not setlists

        if backfill:
            if last_page:
                state.save_cursor(conn, SOURCE, key, keep({"backfill_done": True}))
                break
            page += 1
            state.save_cursor(conn, SOURCE, key, keep({"next_page": page, **({"deep": True} if deep else {})}))
            conn.commit()
        else:
            # Newest first: a page with nothing new means we've caught up.
            if last_page or new_on_page == 0:
                state.save_cursor(conn, SOURCE, key, keep({"backfill_done": True}))
                break
            page += 1
            state.save_cursor(conn, SOURCE, key, keep({"backfill_done": True, "recheck_page": page}))
    else:
        # Budget ran out mid-unit; it stays due and resumes from the cursor.
        return

    nxt = state.mark_checked(conn, SOURCE, key, changed=any_new,
                             min_days=RECHECK_MIN_DAYS, max_days=RECHECK_MAX_DAYS)
    log(f"  {key}: {'backfilled' if backfill else 'checked'}, next {nxt.date()}")
