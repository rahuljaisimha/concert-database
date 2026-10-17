"""Pollstar: upcoming shows with headliner and supporting acts, saved before they vanish.

Pollstar's public JSON only returns events from today onward (past dates are
subscription-gated), so the only way to keep these bills is to store them here
before the show. Endpoints are the ones MusicSceneMap uses (src/api/pollstar.ts):

  Autocomplete  https://autocomplete.pollstar.com/global/All/100/{query}
  Events        https://www.pollstar.com/api/companies/{venueId}/events?...

Request discipline (Pollstar is a site, not an API we have a key for):
  - one request every POLL_INTERVAL seconds, at most DAILY_LIMIT per day
  - a venue's name is looked up once; the match (or no-match) is stored and
    never looked up again for a year
  - each venue's event list is rechecked every 3 days, backing off to 14 days
    when nothing changed (content hash over all pages)

Units of work:
  pollstar / resolve:<our venue id>   find the venue's Pollstar id
  pollstar / venue:<pollstar id>      fetch its upcoming events
"""

import hashlib
import json
import os
import sqlite3
from datetime import date, timedelta

from .. import ledger, state
from ..http import Fetcher
from ..normalize import norm

SOURCE = "pollstar"
AUTOCOMPLETE = "https://autocomplete.pollstar.com/global/All/100/{q}"
EVENTS = "https://www.pollstar.com/api/companies/{id}/events"
POLL_INTERVAL = 2.0
DAILY_LIMIT = 300
PAGE_SIZE = 50
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

def parse_venues(raw: dict) -> list[dict]:
    docs = (raw.get("response") or {}).get("docs") or []
    out = [
        {"id": str(d["EntityId"]), "name": d.get("ListName") or "", "city": d.get("City"),
         "state": d.get("State"), "location": d.get("Location"), "rank": d.get("Rank") or 0}
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
            "name": e.get("name"),
            "url": e.get("url"),
            "is_festival": bool(e.get("isFestival")),
            "artists": [{"name": a["name"].strip(), "headline": bool(a.get("isHeadline"))} for a in artists],
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
    show_id = ledger.find_or_create_show(conn, e["date"], venue_id)
    for pos, (name, role, conf) in enumerate(assign_roles(e["artists"])):
        aid = ledger.get_or_create_artist(conn, name)
        ledger.add_claim(conn, show_id, aid, SOURCE, e["id"], role=role, confidence=conf,
                         position=pos, url=e["url"], extra={"title": e["name"]}, resolve=False)
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
        return self.fetcher.get(SOURCE, EVENTS.format(id=pollstar_id), params=params, headers=self.headers)


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
    resp = client.autocomplete(name)
    state.record_usage(conn, SOURCE)
    if resp.status != 200:
        return None
    v = match_venue(parse_venues(resp.json()), name, city)
    if v:
        _link(conn, v)
    return v


def _link(conn: sqlite3.Connection, v: dict, venue_id: int | None = None) -> int:
    if venue_id is None:
        venue_id = ledger.get_or_create_venue(conn, v["name"], v["city"], v["state"],
                                              source=SOURCE, native_id=v["id"])
    conn.execute("INSERT OR IGNORE INTO venue_source_ids (source, native_id, venue_id) VALUES (?, ?, ?)",
                 (SOURCE, v["id"], venue_id))
    state.ensure(conn, SOURCE, f"venue:{v['id']}", priority=VENUE_PRIORITY)
    return venue_id


def capture(conn: sqlite3.Connection, client: Client, max_requests: int = DAILY_LIMIT, log=print) -> dict:
    stats = {"requests": 0, "resolved": 0, "venues": 0, "unchanged": 0, "events": 0}
    queue_venues_to_resolve(conn)

    def budget_left() -> bool:
        return stats["requests"] < max_requests and state.requests_today(conn, SOURCE) < DAILY_LIMIT

    def spend():
        stats["requests"] += 1
        state.record_usage(conn, SOURCE)

    for row in state.due(conn, SOURCE, limit=10_000):
        if not budget_left():
            break
        kind, ident = row["key"].split(":", 1)
        if kind == "venue":
            _fetch_venue(conn, client, row, ident, stats, spend, budget_left, log)
        elif kind == "resolve":
            _resolve(conn, client, row, int(ident), stats, spend, log)
        conn.commit()
    return stats


def _resolve(conn, client, row, venue_id, stats, spend, log) -> None:
    v = conn.execute("SELECT name, city FROM venues WHERE id = ?", (venue_id,)).fetchone()
    resp = client.autocomplete(v["name"])
    spend()
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


def _fetch_venue(conn, client, row, pollstar_id, stats, spend, budget_left, log) -> None:
    venue = conn.execute("SELECT venue_id FROM venue_source_ids WHERE source = ? AND native_id = ?",
                         (SOURCE, pollstar_id)).fetchone()
    if not venue:
        state.postpone(conn, SOURCE, row["key"], NO_MATCH_DAYS)
        return
    events, digest = [], hashlib.sha256()
    page, total_pages = 0, 1
    while page < min(total_pages, MAX_PAGES):
        if not budget_left():
            return  # stays due; next run refetches from page 0 (lists are small)
        resp = client.events(pollstar_id, page)
        spend()
        if resp.status != 200:
            log(f"  pollstar venue {pollstar_id}: HTTP {resp.status}")
            state.mark_failed(conn, SOURCE, row["key"])
            return
        raw = resp.json()
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
    for e in events:
        ingest_event(conn, e, venue["venue_id"])
    stats["events"] += len(events)
    state.mark_checked(conn, SOURCE, row["key"], changed=True, content_hash=h,
                       min_days=RECHECK_MIN_DAYS, max_days=RECHECK_MAX_DAYS)
    log(f"  pollstar venue {pollstar_id}: {len(events)} upcoming events")
