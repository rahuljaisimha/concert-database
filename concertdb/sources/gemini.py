"""Gemini with Google Search grounding: openers for known shows that lack one.

For a show already in the ledger with at most MAX_ACTS acts listed (default 1:
shows with two or more acts are skipped, even when they may be missing a
second opener; `--max-acts` / GEMINI_MAX_ACTS raises it), Gemini searches the web and answers in JSON: headliner,
openers in order, and a quote from each page that names an opener. The answer
is not trusted as is. Gemini can name pages it didn't read and quotes that
aren't on them (a grounded response may even arrive with no grounding
metadata), so the cited pages are fetched and each act's name is looked for
on them:

  name found on a fetched page        CONF_VERIFIED
  pages fetched, name on none         CONF_ABSENT   (likely made up)
  no page could be fetched            CONF_UNCHECKED

Request discipline (free tier: 1,500 search-grounded requests a day, reset at
midnight Pacific; the other free limits are far above what this uses):
  - at most DAILY_LIMIT grounded requests per Pacific day, counted in `usage`
  - one request at a time, MIN_INTERVAL apart
  - HTTP 429 ends the run (the quota is spent); the show stays due
  - a show is asked once; when nothing is found it is asked again after
    90, 180, then 365 days (pages about a show can appear later)

Unit of work: gemini / show:<our show id>
"""

import json
import re
import sqlite3
from datetime import date

import requests

from .. import db, ledger, state
from ..http import Fetcher
from ..normalize import norm

SOURCE = "gemini"
PAGES = "gemini-pages"      # raw archive for the evidence pages fetched to check answers
API = "https://generativelanguage.googleapis.com/v1beta"
DAILY_LIMIT = 1400          # of the free tier's 1,500; leaves room for smoke tests and hand runs
MIN_INTERVAL = 2.0
PAGE_INTERVAL = 1.0
MAX_PAGES = 3               # evidence pages fetched per answer
MAX_ERRORS = 5              # errors in a row that end the run
MAX_ACTS = 1                # only ask about shows with at most this many acts listed
PROMPT_VERSION = 1
NOT_FOUND_MIN_DAYS = 90
NOT_FOUND_MAX_DAYS = 365
DONE_DAYS = 10 * 365
CONF_VERIFIED = 0.8
CONF_UNCHECKED = 0.5
CONF_ABSENT = 0.2
UNSURE_FACTOR = 0.6         # the model itself said confident: false
SKIP_MODEL_WORDS = ("lite", "image", "tts", "audio", "live", "preview", "exp")


# ---------------------------------------------------------------- parsing

def pick_model(models: list[dict]) -> str | None:
    """Newest stable 'gemini-X.Y-flash' that supports generateContent."""
    best, best_version = None, ()
    for m in models:
        name = (m.get("name") or "").removeprefix("models/")
        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
            continue
        if any(w in name for w in SKIP_MODEL_WORDS):
            continue
        v = re.fullmatch(r"gemini-(\d+(?:\.\d+)*)-flash", name)
        if v:
            version = tuple(int(x) for x in v.group(1).split("."))
            if version > best_version:
                best, best_version = name, version
    return best


def build_prompt(show: dict) -> str:
    d = date.fromisoformat(show["date"])
    place = show["venue"] + (f" in {show['city']}" if show["city"] else "")
    acts = " and ".join(show["acts"])
    return (
        f"A concert at {place} on {d:%B} {d.day}, {d.year} featured {acts}. "
        "Who headlined, and who were the supporting (opening) acts, in billing order? "
        "Use web search. Reply with only JSON: "
        '{"headliner": str, "openers": [str, in order], "confident": bool, '
        '"evidence": [{"url": str, "quote": str}]}. '
        "Quote the exact text from each page that names the opener. "
        "If you can't find it, return an empty openers list rather than guessing."
    )


def read_response(raw: dict) -> dict:
    """Answer text and grounding metadata from a generateContent response."""
    cand = (raw.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    gm = cand.get("groundingMetadata") or {}
    return {
        "text": "".join(p.get("text", "") for p in parts if not p.get("thought")),
        "finish": cand.get("finishReason"),
        "queries": gm.get("webSearchQueries") or [],
        "sources": [c["web"]["uri"] for c in gm.get("groundingChunks") or []
                    if (c.get("web") or {}).get("uri")],
    }


def parse_answer(text: str) -> dict | None:
    """The JSON answer (fenced or bare), or None if it isn't there or has the wrong shape."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    headliner = obj.get("headliner")
    openers = obj.get("openers") or []
    evidence = obj.get("evidence") or []
    if not isinstance(openers, list) or not isinstance(evidence, list):
        return None
    return {
        "headliner": headliner.strip() if isinstance(headliner, str) and headliner.strip() else None,
        "openers": [o.strip() for o in openers if isinstance(o, str) and o.strip()],
        "confident": obj.get("confident") is True,
        "evidence": [{"url": e.get("url") or "", "quote": e.get("quote") or ""}
                     for e in evidence if isinstance(e, dict)],
    }


def page_words(html: str) -> str:
    """Normalized page text (tags dropped, script contents kept: JSON-LD names acts too)."""
    return f" {norm(re.sub(r'<[^>]+>', ' ', html))} "


def mentions(words: str, phrase: str) -> bool:
    n = norm(phrase)
    return bool(n) and f" {n} " in words


# ---------------------------------------------------------------- client

class Client:
    def __init__(self, fetcher: Fetcher, page_fetcher: Fetcher, key: str, model: str | None = None):
        self.fetcher = fetcher
        self.page_fetcher = page_fetcher
        self.headers = {"x-goog-api-key": key}   # a header, so the key never lands in the archive
        self.model = model

    def list_models(self) -> list[dict]:
        models, token = [], None
        while True:
            params = {"pageSize": "1000", **({"pageToken": token} if token else {})}
            resp = self.fetcher.get(SOURCE, f"{API}/models", params=params, headers=self.headers)
            if resp.status != 200:
                raise RuntimeError(f"listing Gemini models: HTTP {resp.status} {resp.text[:300]}")
            page = resp.json()
            models += page.get("models") or []
            token = page.get("nextPageToken")
            if not token:
                return models

    def generate(self, model: str, prompt: str):
        body = {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]}
        return self.fetcher.post_json(SOURCE, f"{API}/models/{model}:generateContent", body,
                                      headers=self.headers)

    def page(self, url: str):
        return self.page_fetcher.get(PAGES, url)


# ------------------------------------------------------------- scheduling

def queue_shows(conn: sqlite3.Connection, max_acts: int = MAX_ACTS) -> int:
    """Past shows with a graph artist and at most max_acts acts on the bill; best-connected first."""
    rows = conn.execute(
        "SELECT s.id, MAX(a.degree) AS degree FROM shows s"
        " JOIN billings b ON b.show_id = s.id JOIN artists a ON a.id = b.artist_id"
        " WHERE s.date < ? AND s.status NOT IN ('cancelled', 'postponed') AND s.is_festival = 0"
        " AND a.in_graph = 1"
        " AND (SELECT COUNT(*) FROM billings b2 WHERE b2.show_id = s.id) <= ?"
        " GROUP BY s.id", (date.today().isoformat(), max_acts)).fetchall()
    for r in rows:
        state.ensure(conn, SOURCE, f"show:{r['id']}", priority=r["degree"])
    return len(rows)


def _show(conn: sqlite3.Connection, show_id: int) -> dict | None:
    s = conn.execute(
        "SELECT s.date, s.is_festival, v.name AS venue, v.city FROM shows s"
        " JOIN venues v ON v.id = s.venue_id WHERE s.id = ?", (show_id,)).fetchone()
    if s is None:
        return None
    bill = conn.execute(
        "SELECT a.name, b.role FROM billings b JOIN artists a ON a.id = b.artist_id"
        " WHERE b.show_id = ? ORDER BY b.position", (show_id,)).fetchall()
    return {"date": s["date"], "venue": s["venue"], "city": s["city"],
            "is_festival": bool(s["is_festival"]), "acts": [b["name"] for b in bill]}


def ask(conn: sqlite3.Connection, client: Client, max_requests: int = DAILY_LIMIT,
        max_acts: int = MAX_ACTS, log=print) -> dict:
    stats = {"requests": 0, "found": 0, "verified": 0, "not_found": 0, "skipped": 0,
             "errors": 0, "search_queries": 0, "stopped": None}
    db.begin_write(conn)
    queue_shows(conn, max_acts)
    conn.commit()

    def budget_left() -> bool:
        return stats["requests"] < max_requests and state.requests_today(conn, SOURCE) < DAILY_LIMIT

    if not budget_left():
        stats["stopped"] = "daily limit"
        return stats
    model = client.model or pick_model(client.list_models())
    if not model:
        raise RuntimeError("no stable Gemini Flash model found; set GEMINI_MODEL")
    stats["model"] = model
    errors = 0
    for row in state.due(conn, SOURCE, limit=10_000):
        if not budget_left():
            stats["stopped"] = "daily limit"
            break
        result = _ask(conn, client, model, row, max_acts, stats, log)
        conn.commit()
        if result == "quota":
            stats["stopped"] = "HTTP 429 (quota spent)"
            break
        errors = errors + 1 if result == "error" else 0
        if errors >= MAX_ERRORS:
            stats["stopped"] = f"{MAX_ERRORS} errors in a row"
            break
    return stats


def _ask(conn, client, model, row, max_acts, stats, log) -> str:
    """One show. Commits before every network call and takes the write lock
    (db.begin_write) only to store what came back; see "Concurrency" in
    docs/ARCHITECTURE.md."""
    key = row["key"]
    show_id = int(key.split(":", 1)[1])
    db.begin_write(conn)
    show = _show(conn, show_id)
    if show is None or len(show["acts"]) > max_acts or show["is_festival"]:
        # Another source filled it in (or max_acts was lowered). Forgotten rather than
        # postponed, so queue_shows brings it back if it qualifies again.
        state.forget(conn, SOURCE, key)
        stats["skipped"] += 1
        return "skipped"

    # Counted (and committed) before the request: a crash can't hide spent quota.
    state.record_usage(conn, SOURCE)
    conn.commit()
    stats["requests"] += 1
    try:
        resp = client.generate(model, build_prompt(show))
    except requests.RequestException as e:
        log(f"  gemini show {show_id}: {type(e).__name__}")
        db.begin_write(conn)
        state.mark_failed(conn, SOURCE, key)
        stats["errors"] += 1
        return "error"
    if resp.status == 429:
        log(f"  gemini: HTTP 429 {resp.text[:300]}")
        return "quota"
    if resp.status != 200:
        log(f"  gemini show {show_id}: HTTP {resp.status} {resp.text[:300]}")
        db.begin_write(conn)
        state.mark_failed(conn, SOURCE, key)
        stats["errors"] += 1
        return "error"

    r = read_response(resp.json())
    stats["search_queries"] += len(r["queries"])
    answer = parse_answer(r["text"])
    if answer is None or not answer["openers"]:
        if answer is None:
            log(f"  gemini show {show_id}: no JSON answer (finishReason {r['finish']})")
        db.begin_write(conn)
        state.mark_checked(conn, SOURCE, key, changed=False,
                           min_days=NOT_FOUND_MIN_DAYS, max_days=NOT_FOUND_MAX_DAYS)
        stats["not_found"] += 1
        return "not_found"

    pages = _fetch_evidence(client, answer, r["sources"])
    db.begin_write(conn)
    acts = ([(answer["headliner"], "headliner")] if answer["headliner"] else []) + [
        (o, "opener") for o in answer["openers"] if norm(o) != norm(answer["headliner"])]
    for pos, (name, role) in enumerate(acts):
        check, url = _check(name, pages)
        conf = {"page": CONF_VERIFIED, "absent": CONF_ABSENT, "unchecked": CONF_UNCHECKED}[check]
        if not answer["confident"]:
            conf *= UNSURE_FACTOR
        quotes = [e["quote"] for e in answer["evidence"] if mentions(f" {norm(e['quote'])} ", name)]
        extra = {"model": model, "prompt_version": PROMPT_VERSION, "check": check,
                 "quote_on_page": any(mentions(p["words"], q) for q in quotes for p in pages),
                 "grounded": bool(r["sources"]), "search_queries": len(r["queries"])}
        aid = ledger.get_or_create_artist(conn, name)
        ledger.add_claim(conn, show_id, aid, SOURCE, key, role=role, confidence=round(conf, 3),
                         position=pos, url=url or _first_url(answer, r["sources"]),
                         extra=extra, resolve=False)
        if role == "opener" and check == "page":
            stats["verified"] += 1
    ledger.resolve_show(conn, show_id)
    state.postpone(conn, SOURCE, key, DONE_DAYS)
    stats["found"] += 1
    log(f"  gemini {show['date']} {show['venue']}: "
        + ", ".join(f"{n} ({role})" for n, role in acts))
    return "found"


def _fetch_evidence(client, answer, sources) -> list[dict]:
    """Fetch the cited pages (the model's evidence first, then grounding sources)."""
    urls = []
    for u in [e["url"] for e in answer["evidence"]] + sources:
        if u.startswith(("http://", "https://")) and u not in urls:
            urls.append(u)
    pages = []
    for u in urls[:MAX_PAGES]:
        try:
            resp = client.page(u)
        except requests.RequestException:
            continue
        if resp.status == 200:
            pages.append({"url": resp.url or u, "words": page_words(resp.text)})
    return pages


def _check(name: str, pages: list[dict]) -> tuple[str, str | None]:
    for p in pages:
        if mentions(p["words"], name):
            return "page", p["url"]
    return ("absent" if pages else "unchecked"), None


def _first_url(answer, sources) -> str | None:
    urls = [e["url"] for e in answer["evidence"] if e["url"]] + sources
    return urls[0] if urls else None
