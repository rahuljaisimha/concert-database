"""Pollstar: upcoming shows with headliner and supporting acts, saved before they vanish.

Pollstar's public JSON only returns events from today onward (past dates are
subscription-gated), so the only way to keep these bills is to store them here
before the show. Endpoints are the ones MusicSceneMap uses (src/api/pollstar.ts):

  Autocomplete  https://autocomplete.pollstar.com/global/All/100/{query}
  Events        https://www.pollstar.com/api/companies/{venueId}/events?...

Request discipline (Pollstar is a site, not an API we have a key for):
  - one request every POLL_INTERVAL seconds, at most DAILY_LIMIT per day
    (POLLSTAR_INTERVAL / POLLSTAR_DAILY_LIMIT in .env)
  - pushback (HTTP 403, 429, 503 or a network error) waits, slows down for the
    rest of the run and retries; several in a row ends the run for the day
  - a venue's name is looked up once; the match (or no-match) is stored and
    never looked up again for a year
  - each venue's event list is rechecked every 3 days, backing off to 14 days
    when nothing changed (content hash over all pages)

Units of work:
  pollstar / resolve:<our venue id>   find the venue's Pollstar id
  pollstar / venue:<pollstar id>      fetch its upcoming events

`concertdb pollstar` runs this as its own long process next to the daily
`concertdb run`; the transaction is committed before every request so the two
processes can share the database (see db.begin_write).
"""

import gzip
import hashlib
import html
import json
import os
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from .. import db, ledger, state
from ..http import Fetcher
from ..normalize import norm

SOURCE = "pollstar"
AUTOCOMPLETE = "https://autocomplete.pollstar.com/global/All/100/{q}"
EVENTS = "https://www.pollstar.com/api/companies/{id}/events"
POLL_INTERVAL = float(os.environ.get("POLLSTAR_INTERVAL") or 1.0)        # seconds, start to start
DAILY_LIMIT = int(os.environ.get("POLLSTAR_DAILY_LIMIT") or 20_000)
MAX_INTERVAL = 10.0
# Pollstar has no published limit, so treat these as "slow down": wait, slow to
# 1.5x the interval for the rest of the run, retry. After len(BACKOFF_SECONDS)
# in a row, stop for the day rather than risk the IP being blocked.
PUSHBACK_STATUSES = (403, 429, 503)
BACKOFF_SECONDS = (30, 60, 120, 300, 600)
PAUSE_HOURS = 12            # after being stopped, no Pollstar requests from any process
sleep = time.sleep          # replaced in tests
DUE_BATCH = 200             # re-read what's due this often, so a long run sees new work
REQUEUE_SECONDS = 1800      # look for newly found venues to resolve every 30 min
# The website asks for 50 events a page; the API accepts more (tested: pageSize
# 100 returned 61 events in one page, 1000 is accepted), so nearly every venue's
# upcoming list is a single request. MAX_PAGES still bounds a runaway listing.
PAGE_SIZE = 1000
MAX_PAGES = 10
YEARS_AHEAD = 2
RECHECK_MIN_DAYS = 3
RECHECK_MAX_DAYS = 14
NO_MATCH_DAYS = 365
RESOLVE_SINCE_YEARS = 3     # only look up venues with shows this recent
RESOLVE_PRIORITY = 1
VENUE_PRIORITY = 1_000        # above any lookup: event lists are where the data is


def user_agent() -> str | None:
    return os.environ.get("POLLSTAR_USER_AGENT")


# ---------------------------------------------------------------- parsing

def _text(s: str | None) -> str | None:
    """Pollstar's autocomplete HTML-escapes names ("Stubb&#39;s", "Bar &amp; Grill")."""
    return html.unescape(s) if s else s


def parse_venues(raw: dict) -> list[dict]:
    docs = (raw.get("response") or {}).get("docs") or []
    out = [
        {"id": str(d["EntityId"]), "name": _text(d.get("ListName")) or "", "city": _text(d.get("City")),
         "state": _text(d.get("State")), "location": _text(d.get("Location")), "rank": d.get("Rank") or 0}
        for d in docs if d.get("EntityType") == "Venue" and d.get("EntityId") is not None
    ]
    return sorted(out, key=lambda v: -v["rank"])


def match_venue(candidates: list[dict], name: str, city: str | None) -> dict | None:
    """Pick the candidate with the same name in the same city; None if unsure."""
    n, c = norm(name), norm(city)
    same_city = [v for v in candidates if not c or norm(v["city"]) == c]
    for v in same_city:
        if norm(v["name"]) == n:
            return v
    loose = [v for v in same_city if n and (n in norm(v["name"]) or norm(v["name"]) in n)]
    return loose[0] if len(loose) == 1 else None


def parse_events(raw: dict) -> list[dict]:
    out = []
    for e in raw.get("events") or []:
        if not e.get("playDate"):
            continue
        artists = [a for a in e.get("artists") or [] if (a.get("name") or "").strip()]
        out.append({
            "id": str(e["id"]),
            "date": str(e["playDate"])[:10],
            "name": _text(e.get("name")),
            "url": e.get("url"),
            "is_festival": bool(e.get("isFestival")),
            "artists": [{"name": _text(a["name"]).strip(), "headline": bool(a.get("isHeadline"))}
                        for a in artists],
        })
    return out


def assign_roles(artists: list[dict]) -> list[tuple[str, str, float]]:
    """(name, role, confidence) in billing order: headliners first, then support."""
    if not artists:
        return []
    if len(artists) == 1:
        return [(artists[0]["name"], "unknown", 0.0)]
    flagged = any(a["headline"] for a in artists)
    if not flagged:  # no flags: Pollstar lists the headliner first
        return [(a["name"], "headliner" if i == 0 else "opener", 0.6) for i, a in enumerate(artists)]
    heads = [a for a in artists if a["headline"]]
    rest = [a for a in artists if not a["headline"]]
    return [(a["name"], "headliner", 0.9) for a in heads] + [(a["name"], "opener", 0.9) for a in rest]


# ----------------------------------------------------------------- ingest

def ingest_event(conn: sqlite3.Connection, e: dict, venue_id: int) -> int:
    acts = [(ledger.get_or_create_artist(conn, name), role, conf)
            for name, role, conf in assign_roles(e["artists"])]
    show_id = ledger.find_or_create_show(conn, e["date"], venue_id, [a for a, _, _ in acts],
                                         source=SOURCE, native_id=e["id"])
    for pos, (aid, role, conf) in enumerate(acts):
        ledger.add_claim(conn, show_id, aid, SOURCE, e["id"], role=role, confidence=conf, position=pos,
                         url=e["url"], extra={"title": e["name"], "date": e["date"]}, resolve=False)
    ledger.resolve_show(conn, show_id)  # recomputes is_festival from act count
    if e["is_festival"]:
        conn.execute("UPDATE shows SET is_festival = 1 WHERE id = ?", (show_id,))
    return show_id


# ---------------------------------------------------------------- client

class Client:
    def __init__(self, fetcher: Fetcher):
        self.fetcher = fetcher
        ua = user_agent()
        self.headers = {"Accept": "application/json", **({"User-Agent": ua} if ua else {})}

    def slow_down(self) -> float:
        self.fetcher.min_interval = min(MAX_INTERVAL, max(self.fetcher.min_interval, 1.0) * 1.5)
        return self.fetcher.min_interval

    def autocomplete(self, query: str):
        from urllib.parse import quote
        return self.fetcher.get(SOURCE, AUTOCOMPLETE.format(q=quote(query, safe="")), headers=self.headers)

    def events(self, pollstar_id: str, page: int):
        today = date.today()
        to = today.replace(year=today.year + YEARS_AHEAD) if not (today.month == 2 and today.day == 29) \
            else today + timedelta(days=365 * YEARS_AHEAD)
        params = {
            "page": str(page), "pageSize": str(PAGE_SIZE),
            "fromDate": today.strftime("%m-%d-%Y"), "toDate": to.strftime("%m-%d-%Y"),
            "sortAscending": "true", "sortColumn": "playdate", "newOnly": "false",
        }
        # Not archived here: _fetch_venue archives the pages only if the list changed.
        return self.fetcher.get(SOURCE, EVENTS.format(id=pollstar_id), params=params, headers=self.headers,
                                keep_raw=False)

    def keep(self, url: str, text: str) -> None:
        """Archive an event-list page (after it turned out to be new)."""
        self.fetcher.archive(SOURCE, url, None, text)


# ------------------------------------------------------------- scheduling

def queue_venues_to_resolve(conn: sqlite3.Connection) -> int:
    """Every venue with a show in the last few years (or upcoming) gets a lookup unit."""
    since = (date.today() - timedelta(days=365 * RESOLVE_SINCE_YEARS)).isoformat()
    rows = conn.execute(
        "SELECT v.id, COUNT(s.id) AS n FROM venues v JOIN shows s ON s.venue_id = v.id"
        " WHERE s.date >= ? AND v.id NOT IN"
        " (SELECT venue_id FROM venue_source_ids WHERE source = ?)"
        " GROUP BY v.id", (since, SOURCE)).fetchall()
    for r in rows:
        # Busier venues first.
        state.ensure(conn, SOURCE, f"resolve:{r['id']}", priority=RESOLVE_PRIORITY + min(r["n"], 500))
    return len(rows)


def add_venue(conn: sqlite3.Connection, client: Client, name: str, city: str | None) -> dict | None:
    """Look a venue up right away (CLI). Returns the Pollstar match or None."""
    conn.commit()
    resp = client.autocomplete(name)
    db.begin_write(conn)
    state.record_usage(conn, SOURCE)
    if resp.status != 200:
        return None
    v = match_venue(parse_venues(resp.json()), name, city)
    if v:
        _link(conn, v)
    return v


def rematch_archived(conn: sqlite3.Connection, data_dir, log=print) -> dict:
    """Retry venue lookups that found no match, using the autocomplete answers already
    in the raw archive (no requests). For lookups made before names were un-escaped:
    "Stubb&#39;s" never matched "Stubb's"."""
    from urllib.parse import unquote, urlparse
    answers: dict[str, str] = {}   # query → newest archived autocomplete body
    for f in sorted((Path(data_dir) / "raw" / SOURCE).glob("*.jsonl.gz")):
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                url = rec.get("url") or ""
                if "autocomplete." in url:
                    answers[unquote(urlparse(url).path.rsplit("/", 1)[-1])] = rec.get("body") or ""
    stats = {"misses": 0, "archived": 0, "matched": 0}
    db.begin_write(conn)
    rows = conn.execute(
        "SELECT cs.key, v.id, v.name, v.city FROM crawl_state cs"
        " JOIN venues v ON cs.key = 'resolve:' || v.id"
        " WHERE cs.source = ? AND cs.interval_days = ? AND v.id NOT IN"
        " (SELECT venue_id FROM venue_source_ids WHERE source = ?)",
        (SOURCE, NO_MATCH_DAYS, SOURCE)).fetchall()
    for r in rows:
        stats["misses"] += 1
        body = answers.get(r["name"])
        if body is None:
            continue
        stats["archived"] += 1
        try:
            match = match_venue(parse_venues(json.loads(body)), r["name"], r["city"])
        except (ValueError, KeyError, TypeError):
            continue
        if match:
            _link(conn, match, r["id"])
            state.postpone(conn, SOURCE, r["key"], 10 * 365)
            stats["matched"] += 1
            log(f"  {r['name']}, {r['city']} → pollstar {match['id']}")
    conn.commit()
    return stats


def _link(conn: sqlite3.Connection, v: dict, venue_id: int | None = None) -> int:
    if venue_id is None:
        venue_id = ledger.get_or_create_venue(conn, v["name"], v["city"], v["state"],
                                              source=SOURCE, native_id=v["id"])
    conn.execute("INSERT OR IGNORE INTO venue_source_ids (source, native_id, venue_id) VALUES (?, ?, ?)",
                 (SOURCE, v["id"], venue_id))
    state.ensure(conn, SOURCE, f"venue:{v['id']}", priority=VENUE_PRIORITY)
    return venue_id


def paused_until(conn: sqlite3.Connection) -> str | None:
    """The pause end time (ISO, UTC) if Pollstar is paused now, else None."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'pollstar_paused_until'").fetchone()
    if row and row["value"] > state._iso(state._now()):
        return row["value"]
    return None


def pause(conn: sqlite3.Connection, hours: float) -> str:
    until = state._iso(state._now() + timedelta(hours=hours))
    db.begin_write(conn)
    conn.execute("INSERT INTO meta (key, value) VALUES ('pollstar_paused_until', ?)"
                 " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (until,))
    return until


class Blocked(Exception):
    """Pollstar pushed back several times in a row: stop for the day."""


def capture(conn: sqlite3.Connection, client: Client, max_requests: int = DAILY_LIMIT, log=print) -> dict:
    """Work through due lookups and event lists until the budget or the work runs out."""
    stats = {"requests": 0, "resolved": 0, "venues": 0, "unchanged": 0, "events": 0,
             "pushback": 0, "_streak": 0}
    until = paused_until(conn)
    if until:
        log(f"pollstar: paused until {until} after repeated pushback; skipping")
        stats.pop("_streak")
        return stats

    def budget_left() -> bool:
        return stats["requests"] < max_requests and state.requests_today(conn, SOURCE) < DAILY_LIMIT

    def get(call, *args):
        """One request, retried after pushback. Commits first: no lock held while waiting."""
        while True:
            conn.commit()
            try:
                resp = call(*args)
            except requests.RequestException as exc:
                resp, why = None, f"{type(exc).__name__}: {exc}"
            db.begin_write(conn)
            stats["requests"] += 1
            state.record_usage(conn, SOURCE)
            if resp is not None and resp.status not in PUSHBACK_STATUSES:
                stats["_streak"] = 0
                return resp
            if resp is not None:
                why = f"HTTP {resp.status} {resp.text[:120]!r}"
            streak = stats["_streak"]
            stats["pushback"] += 1
            if streak >= len(BACKOFF_SECONDS):
                raise Blocked(why)
            wait = max((resp.retry_after if resp is not None else None) or 0, BACKOFF_SECONDS[streak])
            interval = client.slow_down() if hasattr(client, "slow_down") else None
            log(f"  pollstar: {why}; waiting {wait:.0f}s"
                + (f", then {interval:.1f}s between requests" if interval else ""))
            stats["_streak"] = streak + 1
            conn.commit()
            sleep(wait)

    last_queue = None
    try:
        while budget_left():
            if last_queue is None or time.monotonic() - last_queue > REQUEUE_SECONDS:
                db.begin_write(conn)
                queue_venues_to_resolve(conn)
                conn.commit()
                last_queue = time.monotonic()
            rows = state.due(conn, SOURCE, limit=DUE_BATCH)
            if not rows:
                break
            for row in rows:
                if not budget_left():
                    break
                kind, ident = row["key"].split(":", 1)
                if kind == "venue":
                    _fetch_venue(conn, get, client, row, ident, stats, budget_left, log)
                elif kind == "resolve":
                    _resolve(conn, get, client, row, int(ident), stats, log)
                conn.commit()
    except Blocked as exc:
        until = pause(conn, PAUSE_HOURS)
        log(f"pollstar: {len(BACKOFF_SECONDS) + 1} pushbacks in a row ({exc}); paused until {until}")
        conn.commit()
    stats.pop("_streak", None)
    return stats


def _resolve(conn, get, client, row, venue_id, stats, log) -> None:
    v = conn.execute("SELECT name, city FROM venues WHERE id = ?", (venue_id,)).fetchone()
    if not v:
        state.postpone(conn, SOURCE, row["key"], NO_MATCH_DAYS)
        return
    resp = get(client.autocomplete, v["name"])
    if resp.status != 200:
        state.mark_failed(conn, SOURCE, row["key"])
        return
    match = match_venue(parse_venues(resp.json()), v["name"], v["city"])
    if match:
        _link(conn, match, venue_id)
        state.postpone(conn, SOURCE, row["key"], 10 * 365)  # done; never looked up again
        stats["resolved"] += 1
        log(f"  {v['name']}, {v['city']} → pollstar {match['id']}")
    else:
        state.postpone(conn, SOURCE, row["key"], NO_MATCH_DAYS)


def _fetch_venue(conn, get, client, row, pollstar_id, stats, budget_left, log) -> None:
    venue = conn.execute("SELECT venue_id FROM venue_source_ids WHERE source = ? AND native_id = ?",
                         (SOURCE, pollstar_id)).fetchone()
    if not venue:
        state.postpone(conn, SOURCE, row["key"], NO_MATCH_DAYS)
        return
    events, digest, raw_pages = [], hashlib.sha256(), []
    page, total_pages = 0, 1
    while page < min(total_pages, MAX_PAGES):
        if not budget_left():
            return  # stays due; next run refetches from page 0 (lists are small)
        resp = get(client.events, pollstar_id, page)
        if resp.status != 200:
            log(f"  pollstar venue {pollstar_id}: HTTP {resp.status}")
            state.mark_failed(conn, SOURCE, row["key"])
            return
        raw = resp.json()
        raw_pages.append((resp.url or EVENTS.format(id=pollstar_id), resp.text))
        digest.update(resp.text.encode())
        events += parse_events(raw)
        total_pages = int(raw.get("totalPages") or 1)
        page += 1
    stats["venues"] += 1
    h = digest.hexdigest()
    if h == row["content_hash"]:
        stats["unchanged"] += 1
        state.mark_checked(conn, SOURCE, row["key"], changed=False,
                           min_days=RECHECK_MIN_DAYS, max_days=RECHECK_MAX_DAYS)
        return
    if hasattr(client, "keep"):   # unchanged lists are not archived again
        for url, text in raw_pages:
            client.keep(url, text)
    for e in events:
        ingest_event(conn, e, venue["venue_id"])
    stats["events"] += len(events)
    state.mark_checked(conn, SOURCE, row["key"], changed=True, content_hash=h,
                       min_days=RECHECK_MIN_DAYS, max_days=RECHECK_MAX_DAYS)
    log(f"  pollstar venue {pollstar_id}: {len(events)} upcoming events")
