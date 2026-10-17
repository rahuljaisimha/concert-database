"""schema.org event data embedded in venue calendars and ticketing pages.

Most ticketing platforms (DICE, Eventbrite, Tixr, See Tickets, ...) and many
venue sites embed <script type="application/ld+json"> blocks describing each
event as a schema.org MusicEvent with its performers. One parser covers all of
them. Billing order comes from the performer list, or from the event title
("Iguana Death Cult w/ Flat Worms") when only one performer is listed.

Units of work: jsonld / url:<url> — a calendar or event page to recheck.
"""

import json
import re
import sqlite3
from datetime import date

from bs4 import BeautifulSoup

from .. import ledger, state
from ..http import Fetcher

SOURCE = "jsonld"
EVENT_TYPES = {"MusicEvent", "Event", "Festival", "ComedyEvent"}
STATUS_MAP = {
    "EventCancelled": "cancelled",
    "EventPostponed": "postponed",
    "EventRescheduled": "scheduled",
    "EventScheduled": "scheduled",
    "EventMovedOnline": "scheduled",
}
# "Headliner w/ Opener", "Headliner with Opener & Opener 2", "Headliner + Opener"
_TITLE_SPLIT = re.compile(r"\s+(?:w/|with|feat\.?|featuring|\+|support(?:ed)? by|special guests?)\s+", re.I)
# Only commas and "+" separate openers; "&" and "and" are too common inside
# band names ("Florence and the Machine") to split on.
_LIST_SPLIT = re.compile(r"\s*(?:,|\+)\s*")
_TITLE_NOISE = re.compile(r"\s*[\(\[][^)\]]*(?:tour|tickets?|sold out|21\+|18\+|all ages)[^)\]]*[\)\]]", re.I)


def _types(obj: dict) -> set[str]:
    t = obj.get("@type")
    return set(t) if isinstance(t, list) else {t} if t else set()


def _walk(node):
    if isinstance(node, list):
        for x in node:
            yield from _walk(x)
    elif isinstance(node, dict):
        if _types(node) & EVENT_TYPES:
            yield node
        for k in ("@graph", "itemListElement", "item", "subEvent", "event", "events"):
            if k in node:
                yield from _walk(node[k])


def extract_events(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    events = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except json.JSONDecodeError:
            continue
        events.extend(_walk(data))
    return events


def _names(performer) -> list[str]:
    if not performer:
        return []
    if isinstance(performer, (str, dict)):
        performer = [performer]
    out = []
    for p in performer:
        n = p if isinstance(p, str) else p.get("name")
        if n and n.strip():
            out.append(n.strip())
    return out


def split_title(title: str) -> list[str]:
    """'Iguana Death Cult w/ Flat Worms & Guests' → ['Iguana Death Cult', 'Flat Worms']."""
    t = _TITLE_NOISE.sub("", title or "").strip()
    parts = _TITLE_SPLIT.split(t, maxsplit=1)
    acts = [parts[0].strip()]
    if len(parts) > 1:
        acts += [a.strip() for a in _LIST_SPLIT.split(parts[1])]
    return [a for a in acts if a and not re.fullmatch(r"(?:(?:very )?special )?(?:more|guests?|friends|tba|tbd)", a, re.I)]


def parse_event(ev: dict) -> dict | None:
    start = ev.get("startDate") or ""
    if not re.match(r"\d{4}-\d{2}-\d{2}", start):
        return None
    loc = ev.get("location") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    addr = loc.get("address") or {}
    if isinstance(addr, str):
        addr = {"streetAddress": addr}
    country = addr.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name")
    geo = loc.get("geo") or {}

    performers = _names(ev.get("performer"))
    title_acts = split_title(ev.get("name") or "")
    acts = performers if len(performers) >= 2 else (
        title_acts if len(title_acts) >= 2 else performers or title_acts[:1]
    )
    status = ev.get("eventStatus") or ""
    status = STATUS_MAP.get(status.rsplit("/", 1)[-1]) if status else None
    return {
        "date": start[:10],
        "name": ev.get("name"),
        "url": ev.get("url"),
        "venue_name": loc.get("name") or "",
        "city": addr.get("addressLocality"),
        "state": addr.get("addressRegion"),
        "country": country,
        "lat": _float(geo.get("latitude")),
        "lon": _float(geo.get("longitude")),
        "acts": acts,
        "status": status,
    }


def _float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def ingest_event(conn: sqlite3.Connection, e: dict, page_url: str) -> int | None:
    if not e["venue_name"] or not e["acts"]:
        return None
    venue_id = ledger.get_or_create_venue(conn, e["venue_name"], e["city"], e["state"],
                                          e["country"], e["lat"], e["lon"])
    show_id = ledger.find_or_create_show(conn, e["date"], venue_id)
    if e["status"] in ("cancelled", "postponed"):
        ledger.set_show_status(conn, show_id, e["status"])
    native = e["url"] or f"{page_url}#{e['date']}:{e['venue_name']}"
    many = len(e["acts"]) >= 2
    for i, name in enumerate(e["acts"]):
        artist_id = ledger.get_or_create_artist(conn, name)
        role = ("headliner" if i == 0 else "opener") if many else "unknown"
        ledger.add_claim(conn, show_id, artist_id, SOURCE, native, role=role,
                         confidence=0.8 if many else 0.0, position=i, url=e["url"] or page_url,
                         extra={"title": e["name"]}, resolve=False)
    ledger.resolve_show(conn, show_id)
    return show_id


def add_url(conn: sqlite3.Connection, url: str, priority: int = 10) -> None:
    state.ensure(conn, SOURCE, f"url:{url}", priority=priority)


def capture(conn: sqlite3.Connection, fetcher: Fetcher, limit: int = 200, log=print) -> dict:
    """Recheck due calendar/event pages and store every event on them."""
    stats = {"pages": 0, "unchanged": 0, "events": 0}
    for row in state.due(conn, SOURCE, limit=limit):
        url = row["key"].split(":", 1)[1]
        try:
            resp = fetcher.get(SOURCE, url, etag=row["etag"], last_modified=row["last_modified"])
        except Exception as exc:  # network errors: retry later
            log(f"  {url}: {exc}")
            state.mark_failed(conn, SOURCE, row["key"])
            conn.commit()
            continue
        stats["pages"] += 1
        state.record_usage(conn, SOURCE)
        if resp.not_modified or (resp.status == 200 and resp.content_hash == row["content_hash"]):
            stats["unchanged"] += 1
            state.mark_checked(conn, SOURCE, row["key"], changed=False, max_days=_max_days(conn, url))
            conn.commit()
            continue
        if resp.status != 200:
            log(f"  {url}: HTTP {resp.status}")
            state.mark_failed(conn, SOURCE, row["key"])
            conn.commit()
            continue
        n = 0
        for ev in extract_events(resp.text):
            e = parse_event(ev)
            if e and ingest_event(conn, e, url):
                n += 1
        stats["events"] += n
        state.mark_checked(conn, SOURCE, row["key"], changed=True, etag=resp.etag,
                           last_modified=resp.last_modified, content_hash=resp.content_hash,
                           max_days=_max_days(conn, url))
        log(f"  {url}: {n} events")
        conn.commit()
    return stats


def _max_days(conn: sqlite3.Connection, url: str) -> float:
    """Calendars change often; single event pages only until shortly after the show."""
    row = conn.execute(
        "SELECT MAX(s.date) AS d FROM claims c JOIN shows s ON s.id = c.show_id"
        " WHERE c.source = ? AND c.url = ?", (SOURCE, url)
    ).fetchone()
    if row and row["d"] and date.fromisoformat(row["d"]) < date.today():
        return 365  # every event on this page is in the past
    return 7
