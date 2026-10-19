"""Openers for known shows that lack one: web search, fetched pages, Gemini reads them.

Gemini's own Google Search grounding isn't on the free tier for current
models, so the searching is done here and Gemini (no tools) only reads text we
fetched ourselves. Search goes, in SEARCH_ORDER, to context.dev's search API
(CONTEXT_DEV_API_KEY; 2 credits a search with highlights: the passages of each
result relevant to the query, which also reach Facebook event pages) and a local SearXNG (SEARXNG_URL; free;
deploy/searxng.sh runs it), each until it stops answering or runs out, then to
Firecrawl's search API. Work goes by tour, not show:

  1. Take the most urgent due show, and the same artist's other due shows that
     chain to it with gaps of at most TOUR_GAP_DAYS (up to TOUR_MAX_SHOWS).
  2. Search for the tour ("<artist> <countries> <year> tour support") and for
     its venues ("<artist> "<venue 1>" "<venue 2>" ... <year>": this finds the
     promoter's tour page without knowing the promoter). A one-show "tour" gets
     one show search instead.
  3. Fetch the result pages; cut out the text around each show's venue, city
     and date; ONE Gemini call reads it and answers, per show: headliner,
     openers, and a quote that names THIS show together with its openers.
  4. A show is done only when that quote is really on a fetched page, names the
     show (venue, city or date) and contains every opener. A support act
     announced for "all dates" isn't proof for every date, so a show that isn't
     done gets its own search and Gemini call (at most MAX_FALLBACKS per tour).
  5. Shows of the same artist that the pages list but the ledger lacks
     (`other_shows`) are added when their quote is on a fetched page and names
     the venue and the date, inside the tour's dates +- DISCOVER_DAYS.

Claims (source "gemini", weight 0.6):
  act in a show-specific quote found on a page   CONF_SPECIFIC  check "specific"
  act named on a fetched page that names the show CONF_PAGE     check "page"
  act on no fetched page                          not stored (logged)

Budgets: spend what the providers allow, as fast as their per-minute limits
allow. Every request is counted (and committed) before it is sent, so a crash
can't hide spent quota.
  - context.dev (usage source "contextdev", in credits): searches
    CONTEXTDEV_INTERVAL apart (free plan: 10 a minute, 2 at once), with
    highlights (the passages relevant to the query: 2 credits a search). A
    page we can't fetch ourselves (Facebook events) is read from its
    highlights. Out of credits (HTTP 401/402/403, or credits_remaining 0) or 3
    failures in a row switch it off for the run. Only a trimmed answer (no page
    text) is archived.
  - SearXNG (usage source "searxng", in searches): searches SEARXNG_INTERVAL
    apart. It scrapes the search engines it is set to use (deploy/searxng-
    settings.yml), which may block it: after SEARXNG_MAX_FAILS searches in a
    row with no results and engines reported down, the run moves to Firecrawl.
  - Firecrawl (usage source "firecrawl", in credits; a 10-result search costs
    2): searches SEARCH_INTERVAL apart (free plan: 10 a minute). HTTP 402 (out
    of credits) ends the run. Optional cap: FIRECRAWL_MONTHLY_CREDITS a
    calendar month, --max-credits a run.
  - Gemini (usage source "gemini", Pacific day): calls MIN_INTERVAL apart, at
    most DAILY_LIMIT a day. A 429 for the day's quota ends the run.
  - A per-minute 429 (either provider) waits (Retry-After / retryDelay, else
    60 s) and retries, up to RETRIES_429 times; then the run ends. The shows
    stay due.

Unit of work: gemini / show:<our show id>. Done: asked again in 10 years.
Nothing show-specific found: asked again after 90, 180, then 365 days.
"""

import calendar
import html as htmllib
import json
import math
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import requests

from .. import db, ledger, state
from ..http import Fetcher
from ..normalize import norm
from . import setlistfm

SOURCE = "gemini"
SEARCH = "firecrawl"        # usage source for search credits; search responses are archived here
PAGES = "gemini-pages"      # source name for page fetches (not archived)
API = "https://generativelanguage.googleapis.com/v1beta"
SEARCH_API = "https://api.firecrawl.dev/v2/search"
SEARXNG = "searxng"         # usage source for SearXNG searches; responses archived here
CONTEXTDEV = "contextdev"   # usage source for context.dev credits; trimmed responses archived here
CONTEXTDEV_API = "https://api.context.dev/v1/web/search"
CONTEXTDEV_INTERVAL = 6.5   # free plan: 10 requests a minute
CONTEXTDEV_HIGHLIGHT_CHARS = 2000   # highlights: the passages relevant to the query, +1 credit per 10 results
SEARCH_ORDER = ("contextdev", "searxng")   # then Firecrawl; SEARCH_ORDER in .env changes it
SEARXNG_INTERVAL = 15.0     # between SearXNG searches: Google flagged "unusual traffic" after ~100
                            # searches a few seconds apart in an afternoon
SEARXNG_MAX_FAILS = 3       # empty searches in a row (engines down) before falling back to Firecrawl
MODEL = "gemini-3.5-flash-lite"
# Free tier, gemini-3.5-flash-lite: 15 requests/min, 500/day, 250k tokens/min. (gemini-3.8-flash
# is 1/min and 20/day: too few for this.) A prompt is at most ~15k tokens (MAX_PROMPT_CHARS).
DAILY_LIMIT = 500           # Gemini calls per Pacific day
MONTHLY_CREDITS = None      # optional Firecrawl cap per calendar month (FIRECRAWL_MONTHLY_CREDITS)
SEARCH_RESULTS = 10
SEARCH_CREDITS = 2          # what a 10-result search costs; the response says what it really cost
MIN_INTERVAL = 4.5          # between Gemini calls: under 15 a minute (and 250k tokens a minute)
SEARCH_INTERVAL = 6.5       # between searches: under Firecrawl's free 10 a minute
RETRIES_429 = 3             # per-minute pushback: waits before the run gives up
MAX_WAIT_429 = 300          # a longer Retry-After than this ends the run instead
PAGE_INTERVAL = 1.0
MAX_ERRORS = 5              # errors in a row that end the run
MAX_ACTS = 1                # only ask about shows with at most this many acts listed
TOUR_GAP_DAYS = 14
TOUR_MAX_SHOWS = 12
MAX_PAGES = 12              # pages fetched per search round
MAX_FALLBACKS = 4           # per-show searches per tour
VENUE_QUERY_MAX = 4         # venue names in the venue query
EXCERPT_WIDTH = 350         # page text kept on each side of a venue/city/date mention
MAX_PAGE_CHARS = 6000       # excerpt text per page in a prompt
MAX_PROMPT_CHARS = 60000
DISCOVER_DAYS = 14
PROMPT_VERSION = 3
YEAR_CONTEXT_CHARS = 1500   # a listing without a year: the nearest year this far above it on the page counts
NEAR_CHARS = 400            # "page" evidence: the act this close to the artist or the show's date
HANDLE_SUFFIXES = ("band", "music", "official")   # @flatwormsband -> Flat Worms
MAX_OPENERS = 6             # more support acts than this is a festival lineup, not one bill
TRANSIENT = (500, 502, 503, 504)   # "overloaded": waited out like a per-minute 429
TRANSIENT_WAIT = 30
NOT_FOUND_MIN_DAYS = 90
NOT_FOUND_MAX_DAYS = 365
DONE_DAYS = 10 * 365
CONF_SPECIFIC = 0.8
CONF_PAGE = 0.5
PRIORITIZED = 10**6         # added to the queue priority of shows by artists you prioritized

MONTHS = [m.lower() for m in calendar.month_name[1:]]


# ---------------------------------------------------------------- text

def page_text(raw: str) -> str:
    """Visible page text, whitespace collapsed."""
    t = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw or "")
    t = htmllib.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t).strip()


def _padded(s: str) -> str:
    return f" {norm(s)} "


def mentions(words: str, phrase: str) -> bool:
    """words: _padded() text."""
    n = norm(phrase)
    return bool(n) and f" {n} " in words


def date_forms(day: str, numeric: bool = False) -> list[str]:
    """Ways a page writes a date: 'march 2', '2 mar', ... (+ '2/3', '3/2' if numeric)."""
    d = date.fromisoformat(day)
    mon, n = MONTHS[d.month - 1], str(d.day)
    forms = [f"{mon} {n}", f"{n} {mon}", f"{mon[:3]} {n}", f"{n} {mon[:3]}",
             *([f"sept {n}", f"{n} sept"] if d.month == 9 else []),
             f"{mon} {n}th", f"{n}th {mon}", f"{mon} {n}st", f"{n}st {mon}",
             f"{mon} {n}nd", f"{n}nd {mon}", f"{mon} {n}rd", f"{n}rd {mon}"]
    if numeric:
        forms += [f"{n}/{d.month}", f"{d.month}/{n}", f"{d.day:02d}.{d.month:02d}", f"{d.month:02d}.{d.day:02d}"]
    return forms


def venue_core(venue: str) -> str:
    return norm(venue)          # norm() already drops a leading "The"


ARTICLES = {"le", "la", "les", "l", "el", "los", "las", "il", "de", "het", "die", "der"}
GENERIC = {"ballroom", "theatre", "theater", "hall", "club", "bar", "music", "room", "lounge",
           "cafe", "venue", "arena", "amphitheatre", "amphitheater", "center", "centre", "live"}


def venue_keys(venue: str) -> list[str]:
    """How a quote may name a venue: 'Le Transbordeur' -> 'transbordeur',
    'Teragram Ballroom' -> 'teragram' (trailing generic words off, if what's left
    is still a name)."""
    words = venue_core(venue).split()
    keys = [" ".join(words)]
    while words and words[0] in ARTICLES and len(words) > 1:
        words = words[1:]
    keys.append(" ".join(words))
    while len(words) > 1 and words[-1] in GENERIC:
        words = words[:-1]
    if len(" ".join(words)) >= 4:
        keys.append(" ".join(words))
    return list(dict.fromkeys(k for k in keys if k))


def show_keys(show: dict, numeric: bool = False, city: bool = True) -> list[str]:
    """Normalized phrases that point at this show on a page."""
    keys = [venue_core(show["venue"])] + [norm(f) for f in date_forms(show["date"], numeric)]
    if city and show.get("city"):
        keys.append(norm(show["city"]))
    return [k for k in dict.fromkeys(keys) if k]


def raw_keys(show: dict) -> list[str]:
    """Lowercase phrases searched for in raw page text, to cut excerpts."""
    keys = [show["venue"].lower().replace("’", "'"), venue_core(show["venue"])]
    if show.get("city"):
        keys.append(show["city"].lower())
    keys += date_forms(show["date"], numeric=True)
    return [k for k in dict.fromkeys(keys) if k]


def excerpts(text: str, shows: list[dict], width: int = EXCERPT_WIDTH, limit: int = MAX_PAGE_CHARS) -> str:
    """The parts of a page around any of the shows' venue, city or date, merged."""
    low = text.lower().replace("’", "'")
    hits = sorted({m.start() for s in shows for k in raw_keys(s)
                   for m in re.finditer(r"(?<!\w)" + re.escape(k) + r"(?!\w)", low)})
    windows = []
    for h in hits:
        if windows and h - width <= windows[-1][1]:
            windows[-1][1] = h + width
        else:
            windows.append([max(0, h - width), h + width])
    out, total = [], 0
    for a, b in windows:
        piece = text[a:b]
        if total + len(piece) > limit:
            piece = piece[: max(0, limit - total)]
        if piece:
            out.append(piece)
            total += len(piece)
        if total >= limit:
            break
    return " ... ".join(out)


def names_show(words: str, show: dict) -> bool:
    """Does a page (_padded text) name this show's venue or date (a city alone isn't enough)?"""
    return any(f" {k} " in words for k in show_keys(show, city=False))


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WEEKDAY_WORDS = {w: i for i, d in enumerate(WEEKDAYS) for w in (d, d[:3])}
_WEEKDAY_WORDS.update({"tues": 1, "weds": 2, "thur": 3, "thurs": 3})


def quote_years(quote: str) -> set[int]:
    """Years a quote states: 2019, or the year of 9/17/19, 17.09.2019."""
    years = {int(y) for y in re.findall(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)", quote or "")}
    for y in re.findall(r"(?<!\d)\d{1,2}[/.]\d{1,2}[/.](\d{2}|\d{4})(?!\d)", quote or ""):
        years.add(int(y) if len(y) == 4 else 2000 + int(y))
    return years


def quote_weekdays(quote: str, acts=()) -> set[int]:
    """Weekdays a quote names, not counting act names (a band called Wednesday)."""
    q = _padded(quote)
    for a in acts:
        if norm(a):
            q = q.replace(f" {norm(a)} ", " ")
    return {_WEEKDAY_WORDS[w] for w in q.split() if w in _WEEKDAY_WORDS}


def date_contradicted(quote: str, day: str, acts=()) -> bool:
    """The quote states another year, or another weekday, than this date."""
    d = date.fromisoformat(day)
    years, weekdays = quote_years(quote), quote_weekdays(quote, acts)
    return bool(years and d.year not in years) or bool(weekdays and d.weekday() not in weekdays)


def date_confirmed(quote: str, day: str, acts=()) -> bool:
    """The quote pins the year down: it states it, or names the right weekday (a
    listing without either may be from any year's tour)."""
    d = date.fromisoformat(day)
    return not date_contradicted(quote, day, acts) and (
        d.year in quote_years(quote) or d.weekday() in quote_weekdays(quote, acts))


def year_from_context(words: str, quote: str, day: str) -> bool:
    """A listing line without a year ("Feb. 28 - Bangalow @ A & I Hall") under a
    heading or article that gives it ("2026 tour dates"): the nearest year above
    the line on the page (words: _padded page text) is the show's year."""
    parts = [norm(p) for p in re.split(r"\.\.\.|\u2026", quote or "") if norm(p)]
    i = words.find(f" {parts[0]} ") if parts else -1
    if i < 0:
        return False
    years = re.findall(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)", words[max(0, i - YEAR_CONTEXT_CHARS): i])
    return bool(years) and int(years[-1]) == date.fromisoformat(day).year


def _positions(words: str, phrase: str) -> list[int]:
    n = norm(phrase)
    return [m.start() for m in re.finditer(re.escape(f" {n} "), words)] if n else []


def near(words: str, name: str, anchors, dist: int = NEAR_CHARS) -> bool:
    """Is the name mentioned within dist characters of one of the anchors?"""
    spots = [p for a in anchors for p in _positions(words, a)]
    return any(abs(p - q) <= dist for p in _positions(words, name) for q in spots)


def names_this_show(quote: str, show: dict, acts=()) -> bool:
    """A show-specific quote: it names the venue, or the date with the city or the
    year/weekday (the date alone at a residency), and doesn't state another year
    or weekday."""
    if not quote or date_contradicted(quote, show["date"], acts):
        return False
    q = _padded(quote)
    has = lambda keys: any(f" {k} " in q for k in keys if k)     # noqa: E731
    on_date = has([norm(f) for f in date_forms(show["date"], numeric=True)])
    if show.get("need_date"):
        return on_date
    return has(venue_keys(show["venue"])) or (on_date and (
        has([norm(show.get("city"))]) or date_confirmed(quote, show["date"], acts)))


# ---------------------------------------------------------------- prompt / answer

ANSWER_SHAPE = (
    '{"shows": [{"date": "YYYY-MM-DD", "festival": bool, "headliner": str|null, "openers": [str], '
    '"show_specific_quote": str|null, "url": str|null}], '
    '"other_shows": [{"date": "YYYY-MM-DD", "festival": bool, "venue": str, "city": str|null, '
    '"country": str|null, "headliner": str|null, "openers": [str], "quote": str, "url": str}]}'
)


def build_prompt(artist: str, shows: list[dict], pages: list[dict], snippets: list[dict]) -> str:
    lines = [
        f"{artist} played the shows listed below. Using ONLY the text below (not what you know), "
        "give for EACH listed show its headliner and its supporting (opening) acts in billing order.",
        "Reply with only JSON: " + ANSWER_SHAPE,
        "show_specific_quote: copied exactly from the text, a passage that names THIS show (its venue, "
        "city or date) together with its support acts; null if the acts are only given for the tour in "
        "general. url: the page it is from.",
        f"other_shows: shows by {artist} that the text lists but that are NOT in the list below, each "
        "with a quote copied exactly from the text that names its date and venue (the venue's own name, "
        "not just the city; leave out shows whose venue isn't given). Leave it empty if none.",
        "festival: true if the show was a festival or multi-stage event (a lineup, not one bill); then "
        "headliner is its top-billed act and openers the rest of the lineup in billing order.",
        "Write act names as the bill spells them, not social-media handles (\"Flat Worms\", not "
        "\"@flatwormsband\").",
        "Don't guess: an empty openers list is fine. Don't include the headliner among the openers.",
        "", "SHOWS:",
    ]
    for s in shows:
        d = date.fromisoformat(s["date"])
        place = ", ".join(x for x in (s["venue"], s.get("city"), s.get("country")) if x)
        lines.append(f"- {s['date']} ({d:%A}): {place}")
    lines += ["", "PAGE TEXT:"]
    budget = MAX_PROMPT_CHARS
    for p in pages:
        if not p["excerpt"] or budget <= 0:
            continue
        block = f"[{p['url']}]\n{p['excerpt'][:budget]}\n"
        budget -= len(block)
        lines.append(block)
    if snippets:
        lines += ["SEARCH RESULTS:"] + [f"[{x['url']}] {x['title']} — {x['description']}" for x in snippets]
    return "\n".join(lines)


def read_response(raw: dict) -> dict:
    """Answer text (thoughts dropped) and token count from a generateContent response."""
    cand = (raw.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    return {
        "text": "".join(p.get("text", "") for p in parts if not p.get("thought")),
        "finish": cand.get("finishReason"),
        "tokens": (raw.get("usageMetadata") or {}).get("totalTokenCount") or 0,
    }


def _str(v) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _names(v) -> list[str]:
    return [o.strip() for o in v if isinstance(o, str) and o.strip()] if isinstance(v, list) else []


def parse_answer(text: str) -> dict | None:
    """{"shows": {date: {...}}, "other_shows": [...]}, or None if the JSON isn't there."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("shows") or [], list):
        return None
    shows = {}
    for s in obj.get("shows") or []:
        if isinstance(s, dict) and _str(s.get("date")):
            shows[s["date"].strip()] = {
                "headliner": _str(s.get("headliner")), "openers": _names(s.get("openers")),
                "festival": s.get("festival") is True,
                "quote": _str(s.get("show_specific_quote")), "url": _str(s.get("url"))}
    others = []
    for s in obj.get("other_shows") or [] if isinstance(obj.get("other_shows"), list) else []:
        if isinstance(s, dict) and _str(s.get("date")) and _str(s.get("venue")) and _str(s.get("quote")):
            others.append({
                "date": s["date"].strip(), "venue": s["venue"].strip(), "city": _str(s.get("city")),
                "country": _str(s.get("country")), "headliner": _str(s.get("headliner")),
                "openers": _names(s.get("openers")), "festival": s.get("festival") is True,
                "quote": s["quote"].strip(), "url": _str(s.get("url"))})
    return {"shows": shows, "other_shows": others}


def quote_source(quote: str | None, texts: list[dict]) -> dict | None:
    """The text (page or search result) that really contains the quote. The quote
    may skip text with '...'; every part must then be in the same text, in order."""
    parts = [norm(p) for p in re.split(r"\.\.\.|…", quote or "")]
    parts = [p for p in parts if p]
    if not parts:
        return None
    for t in texts:
        pos = 0
        for p in parts:
            i = t["words"].find(f" {p} ", pos)
            if i < 0:
                break
            pos = i + len(p) + 1
        else:
            return t
    return None


# ---------------------------------------------------------------- client

class Client:
    def __init__(self, fetcher: Fetcher, search_fetcher: Fetcher, page_fetcher: Fetcher,
                 key: str, search_key: str | None, model: str | None = None,
                 searxng_url: str | None = None, searxng_fetcher: Fetcher | None = None,
                 contextdev_key: str | None = None, contextdev_fetcher: Fetcher | None = None,
                 search_order=SEARCH_ORDER):
        self.fetcher = fetcher
        self.search_fetcher = search_fetcher
        self.page_fetcher = page_fetcher
        self.headers = {"x-goog-api-key": key}      # headers, so keys never land in the archive
        self.search_headers = {"Authorization": f"Bearer {search_key}"} if search_key else None
        self.model = model or MODEL
        self.searxng_url = searxng_url.rstrip("/") if searxng_url else None
        self.searxng_fetcher = searxng_fetcher or search_fetcher
        self.contextdev_headers = {"Authorization": f"Bearer {contextdev_key}"} if contextdev_key else None
        self.contextdev_fetcher = contextdev_fetcher or search_fetcher
        self.search_order = tuple(search_order)

    @property
    def has_contextdev(self) -> bool:
        return self.contextdev_headers is not None

    def contextdev(self, query: str):
        """Search; the answer carries each result page's markdown. Archived without it
        (10 pages of text per search would be most of the archive)."""
        body = {"query": query, "numResults": SEARCH_RESULTS,
                "highlightsOptions": {"enabled": True, "maxCharacters": CONTEXTDEV_HIGHLIGHT_CHARS}}
        resp = self.contextdev_fetcher.post_json(CONTEXTDEV, CONTEXTDEV_API, body,
                                                 headers=self.contextdev_headers, timeout=90, keep_raw=False)
        if resp.status == 200:
            try:
                raw = resp.json()
                for r in raw.get("results") or []:
                    for k in ("markdown", "highlights"):       # keep the status code, drop the text
                        if isinstance(r.get(k), dict):
                            r[k] = {"code": r[k].get("code")}
                self.contextdev_fetcher.archive(CONTEXTDEV, CONTEXTDEV_API, body, json.dumps(raw))
            except (ValueError, AttributeError):
                pass
        return resp

    @property
    def has_firecrawl(self) -> bool:
        return self.search_headers is not None

    def searxng(self, query: str):
        return self.searxng_fetcher.get(SEARXNG, f"{self.searxng_url}/search",
                                        params={"q": query, "format": "json"}, timeout=60)

    def generate(self, model: str, prompt: str):
        body = {"contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
        return self.fetcher.post_json(SOURCE, f"{API}/models/{model}:generateContent", body,
                                      headers=self.headers)

    def search(self, query: str):
        return self.search_fetcher.post_json(SEARCH, SEARCH_API, {"query": query, "limit": SEARCH_RESULTS},
                                             headers=self.search_headers, timeout=60)

    def page(self, url: str):
        # Whole pages would be most of the archive; claims keep the quote instead.
        return self.page_fetcher.get(PAGES, url, keep_raw=False)


def client_from_env(data_dir, model: str | None = None, env=None) -> "Client":
    """The client the CLI, smoke test and eval use: GEMINI_API_KEY, plus SEARXNG_URL
    and/or FIRECRAWL_API_KEY for search."""
    env = os.environ if env is None else env
    key = env.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("set GEMINI_API_KEY (get one at https://aistudio.google.com/apikey)")
    search_key, searxng_url = (env.get("FIRECRAWL_API_KEY") or "").strip() or None, env.get("SEARXNG_URL") or None
    contextdev_key = (env.get("CONTEXT_DEV_API_KEY") or "").strip() or None
    if not (search_key or searxng_url or contextdev_key):
        raise SystemExit("set CONTEXT_DEV_API_KEY, SEARXNG_URL (a local SearXNG: deploy/searxng.sh) and/or"
                         " FIRECRAWL_API_KEY: Gemini reads the pages a search finds")
    order = [b.strip() for b in (env.get("SEARCH_ORDER") or ",".join(SEARCH_ORDER)).split(",") if b.strip()]
    return Client(Fetcher(data_dir, min_interval=MIN_INTERVAL),
                  Fetcher(data_dir, min_interval=SEARCH_INTERVAL),
                  Fetcher(data_dir, min_interval=PAGE_INTERVAL),
                  key, search_key, model=model or env.get("GEMINI_MODEL") or None,
                  searxng_url=searxng_url, searxng_fetcher=Fetcher(data_dir, min_interval=SEARXNG_INTERVAL),
                  contextdev_key=contextdev_key, contextdev_fetcher=Fetcher(data_dir, min_interval=CONTEXTDEV_INTERVAL),
                  search_order=order)


def searxng_results(raw: dict) -> tuple[list[dict], list[str]]:
    """(results, engines reported down) from a SearXNG JSON answer."""
    results = [{"url": x["url"], "title": x.get("title") or "", "description": x.get("content") or ""}
               for x in raw.get("results") or [] if isinstance(x, dict) and x.get("url")]
    down = [e[0] for e in raw.get("unresponsive_engines") or [] if isinstance(e, list) and e]
    return results[:SEARCH_RESULTS], down


def contextdev_results(raw: dict) -> list[dict]:
    """[{url, title, description, markdown, highlights}] from a context.dev search answer.
    markdown: the page's text, when asked for and scraped; highlights: the passages
    relevant to the query (both "" otherwise)."""
    out = []
    for x in raw.get("results") or []:
        if not (isinstance(x, dict) and x.get("url")):
            continue
        md, hl = x.get("markdown"), x.get("highlights")
        md = (md.get("markdown") if md.get("code") == "SUCCESS" else None) if isinstance(md, dict) else md
        hl = (hl.get("highlights") if hl.get("code") == "SUCCESS" else None) if isinstance(hl, dict) else hl
        out.append({"url": x["url"], "title": x.get("title") or "", "description": x.get("description") or "",
                    "markdown": md if isinstance(md, str) else "",
                    "highlights": " ... ".join(h for h in hl if isinstance(h, str)) if isinstance(hl, list) else ""})
    return out[:SEARCH_RESULTS]


def search_results(raw: dict) -> list[dict]:
    data = raw.get("data")
    web = data.get("web") if isinstance(data, dict) else data
    return [{"url": x["url"], "title": x.get("title") or "", "description": x.get("description") or ""}
            for x in web or [] if isinstance(x, dict) and x.get("url")]


# ------------------------------------------------------------- budgets

def credits_this_month(conn: sqlite3.Connection, today: date | None = None) -> int:
    today = today or datetime.now(timezone.utc).date()
    row = conn.execute("SELECT COALESCE(SUM(requests), 0) AS n FROM usage WHERE source = ? AND day LIKE ?",
                       (SEARCH, f"{today:%Y-%m}-%")).fetchone()
    return row["n"]


def retry_wait(resp) -> float:
    """Seconds to wait after a 429: Retry-After, Gemini's retryDelay, else 60."""
    if resp.retry_after:
        return resp.retry_after
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', resp.text or "")
    return float(m.group(1)) + 1 if m else 60.0


def daily_quota_spent(resp) -> bool:
    """A Gemini 429 for the day's quota (not the minute's)."""
    return "PerDay" in (resp.text or "")


# ------------------------------------------------------------- scheduling

def queue_shows(conn: sqlite3.Connection, max_acts: int = MAX_ACTS) -> int:
    """Past non-festival shows with a graph artist and at most max_acts acts.

    Shows by artists you prioritized come first, then the most recent: pages
    about recent shows are the easiest to find (and well-connected artists with
    long histories, like Pigface, no longer crowd out everyone else). Set, not
    raised, so a change of formula reorders what's already queued."""
    today = date.today()
    rows = conn.execute(
        "SELECT s.id, s.date, MAX(COALESCE(cs.priority, 0)) AS artist_priority FROM shows s"
        " JOIN billings b ON b.show_id = s.id JOIN artists a ON a.id = b.artist_id"
        " LEFT JOIN crawl_state cs ON cs.source = 'setlistfm' AND cs.key = 'artist:' || a.id"
        " WHERE s.date < ? AND s.status NOT IN ('cancelled', 'postponed') AND s.is_festival = 0"
        " AND a.in_graph = 1"
        " AND (SELECT COUNT(*) FROM billings b2 WHERE b2.show_id = s.id) <= ?"
        " GROUP BY s.id", (today.isoformat(), max_acts)).fetchall()
    epoch = date(2000, 1, 1)
    for r in rows:
        prio = max(0, (date.fromisoformat(r["date"]) - epoch).days)
        if r["artist_priority"] >= setlistfm.NO_CAP_PRIORITY:   # seed.USER_PRIORITY
            prio += PRIORITIZED
        key = f"show:{r['id']}"
        state.ensure(conn, SOURCE, key, priority=prio)
        conn.execute("UPDATE crawl_state SET priority = ? WHERE source = ? AND key = ?", (prio, SOURCE, key))
    return len(rows)


def _show(conn: sqlite3.Connection, show_id: int) -> dict | None:
    s = conn.execute(
        "SELECT s.id, s.date, s.is_festival, s.status, v.name AS venue, v.city, v.country FROM shows s"
        " JOIN venues v ON v.id = s.venue_id WHERE s.id = ?", (show_id,)).fetchone()
    if s is None:
        return None
    bill = conn.execute(
        "SELECT a.id, a.name, a.in_graph FROM billings b JOIN artists a ON a.id = b.artist_id"
        " WHERE b.show_id = ? ORDER BY b.position IS NULL, b.position, a.name", (show_id,)).fetchall()
    lead = next((b for b in bill if b["in_graph"]), bill[0] if bill else None)
    return {"id": s["id"], "date": s["date"], "venue": s["venue"], "city": s["city"],
            "country": s["country"], "is_festival": bool(s["is_festival"]), "status": s["status"],
            "acts": [b["name"] for b in bill],
            "lead_id": lead["id"] if lead else None, "lead": lead["name"] if lead else None}


def _qualifies(show: dict | None, max_acts: int) -> bool:
    return bool(show and show["lead_id"] and len(show["acts"]) <= max_acts and not show["is_festival"]
                and show["status"] not in ("cancelled", "postponed"))


def tour_of(conn: sqlite3.Connection, seed_show: dict, max_acts: int, exclude: set) -> list[dict]:
    """The seed show plus the lead artist's other due, qualifying shows that chain
    to it with gaps of at most TOUR_GAP_DAYS; at most TOUR_MAX_SHOWS, nearest first."""
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    ids = [r["id"] for r in conn.execute(
        "SELECT DISTINCT s.id FROM shows s JOIN billings b ON b.show_id = s.id"
        " JOIN crawl_state cs ON cs.source = ? AND cs.key = 'show:' || s.id"
        " WHERE b.artist_id = ? AND cs.next_due <= ? AND s.date BETWEEN ? AND ?",
        (SOURCE, seed_show["lead_id"], now,
         (date.fromisoformat(seed_show["date"]) - timedelta(days=TOUR_GAP_DAYS * TOUR_MAX_SHOWS)).isoformat(),
         (date.fromisoformat(seed_show["date"]) + timedelta(days=TOUR_GAP_DAYS * TOUR_MAX_SHOWS)).isoformat()))]
    cands = {seed_show["id"]: seed_show}
    for i in ids:
        if i not in cands and i not in exclude:
            s = _show(conn, i)
            if _qualifies(s, max_acts) and s["lead_id"] == seed_show["lead_id"]:
                cands[i] = s
    ordered = sorted(cands.values(), key=lambda s: (s["date"], s["id"]))
    k = next(i for i, s in enumerate(ordered) if s["id"] == seed_show["id"])
    lo = hi = k

    def gap(a, b):
        return (date.fromisoformat(b["date"]) - date.fromisoformat(a["date"])).days

    while hi - lo + 1 < TOUR_MAX_SHOWS:
        left = gap(ordered[lo - 1], ordered[lo]) if lo > 0 else None
        right = gap(ordered[hi], ordered[hi + 1]) if hi + 1 < len(ordered) else None
        left = left if left is not None and left <= TOUR_GAP_DAYS else None
        right = right if right is not None and right <= TOUR_GAP_DAYS else None
        if left is None and right is None:
            break
        if right is None or (left is not None and left <= right):
            lo -= 1
        else:
            hi += 1
    return ordered[lo: hi + 1]


# ------------------------------------------------------------- queries

def show_query(s: dict) -> str:
    d = date.fromisoformat(s["date"])
    city = f" {s['city']}" if s.get("city") else ""
    return f'"{s["venue"]}"{city} {s["lead"]} {d:%B} {d.day} {d.year} support'


def tour_queries(tour: list[dict]) -> list[str]:
    if len(tour) == 1:
        return [show_query(tour[0])]
    artist, year = tour[0]["lead"], tour[0]["date"][:4]
    countries = " ".join(sorted({s["country"] for s in tour if s.get("country")}))
    venues = []
    for s in tour:                      # spread over the tour: first, last, then the rest
        if s["venue"] not in venues:
            venues.append(s["venue"])
    if len(venues) > VENUE_QUERY_MAX:
        step = (len(venues) - 1) / (VENUE_QUERY_MAX - 1)
        venues = [venues[round(i * step)] for i in range(VENUE_QUERY_MAX)]
    return [" ".join(x for x in (artist, countries, year, "tour support") if x),
            f"{artist} " + " ".join(f'"{v}"' for v in venues) + f" {year}"]


# ------------------------------------------------------------- the run

class Stop(Exception):
    """Ends the run: a quota is spent."""


class Run:
    def __init__(self, conn, client, max_requests, max_credits, monthly_credits, max_acts, log):
        self.conn, self.client, self.log = conn, client, log
        self.max_requests, self.max_credits = max_requests, max_credits
        self.monthly, self.max_acts = monthly_credits, max_acts
        self.model = client.model or MODEL
        self.searxng_on = bool(getattr(client, "searxng_url", None))
        self.searxng_fails = 0
        self.firecrawl_on = getattr(client, "has_firecrawl", True)
        self.contextdev_on = getattr(client, "has_contextdev", False)
        self.contextdev_fails = 0
        self.order = tuple(getattr(client, "search_order", SEARCH_ORDER))
        self.stats = {"model": self.model, "tours": 0, "shows": 0, "searches": 0, "searxng": 0,
                      "contextdev": 0, "contextdev_credits": 0, "credits": 0,
                      "pages": 0, "gemini_calls": 0, "tokens": 0, "specific": 0, "page_only": 0,
                      "not_found": 0, "festival": 0, "discovered": 0, "skipped": 0, "errors": 0, "stopped": None}
        self.pages: dict[str, dict] = {}      # url -> {"url", "text", "words"}; fetched once per run
        self.backup_text: dict[str, str] = {}  # url -> the search's passages from it, if fetching fails
        self.sleep = time.sleep

    # budgets
    def credits_left(self) -> int:
        left = [math.inf]
        if self.max_credits is not None:
            left.append(self.max_credits - self.stats["credits"])
        if self.monthly is not None:
            left.append(self.monthly - credits_this_month(self.conn))
        return min(left)

    def calls_left(self) -> int:
        return min(self.max_requests - self.stats["gemini_calls"],
                   DAILY_LIMIT - state.requests_today(self.conn, SOURCE))

    def can_afford(self, searches: int) -> bool:
        if self.calls_left() < 1:
            return False
        return self.searxng_on or self.contextdev_on or (
            self.firecrawl_on and self.credits_left() >= searches * SEARCH_CREDITS)

    # requests
    def call(self, send, name: str):
        """send(), waiting out per-minute 429s and "overloaded" 5xx; Stop when a quota is spent."""
        for attempt in range(RETRIES_429 + 1):
            resp = send()
            if resp.status in TRANSIENT and attempt < RETRIES_429:
                self.log(f"  {name.lower()}: HTTP {resp.status}, waiting {TRANSIENT_WAIT}s")
                self.sleep(TRANSIENT_WAIT)
                continue
            if resp.status != 429:
                return resp
            wait = retry_wait(resp)
            if (name == "Gemini" and daily_quota_spent(resp)) or wait > MAX_WAIT_429 or attempt == RETRIES_429:
                self.log(f"  {name.lower()}: HTTP 429 {resp.text[:300]}")
                raise Stop(f"{name} HTTP 429 (quota spent)")
            self.log(f"  {name.lower()}: HTTP 429, waiting {wait:.0f}s")
            self.sleep(wait)

    def search(self, query: str) -> list[dict]:
        """The first search in self.order that answers (context.dev, SearXNG), else Firecrawl."""
        for backend in self.order:
            results = None
            if backend == "contextdev" and self.contextdev_on:
                results = self.search_contextdev(query)
            elif backend == "searxng" and self.searxng_on:
                results = self.search_searxng(query)
            if results is not None:
                return results
        if not self.firecrawl_on:
            raise Stop("no search left: SearXNG / context.dev aren't answering and there is no FIRECRAWL_API_KEY")
        if self.credits_left() < SEARCH_CREDITS:
            raise Stop("budget spent")
        return self.search_firecrawl(query)

    def search_contextdev(self, query: str) -> list[dict] | None:
        """Results (their page text kept as fetched pages), or None when context.dev
        failed; out of credits, or 3 failures in a row, switch it off for the run."""
        db.begin_write(self.conn)
        state.record_usage(self.conn, CONTEXTDEV, 1)
        self.conn.commit()
        self.stats["searches"] += 1
        self.stats["contextdev"] += 1
        self.stats["contextdev_credits"] += 1
        try:
            resp = self.call(lambda: self.client.contextdev(query), "context.dev")
        except Stop as e:
            resp, why = None, str(e)
        except requests.RequestException as e:
            resp, why = None, type(e).__name__
        if resp is not None and resp.status in (401, 402, 403):
            self.contextdev_on = False
            self.log(f"  context.dev: HTTP {resp.status} {resp.text[:200]}; switched off for this run")
            return None
        raw = None
        if resp is not None and resp.status == 200:
            try:
                raw = resp.json()
            except ValueError:
                why = "not JSON"
        elif resp is not None:
            why = f"HTTP {resp.status} {resp.text[:120]}"
        if raw is None:
            self.contextdev_fails += 1
            self.log(f"  context.dev: {why}")
            if self.contextdev_fails >= 3:
                self.contextdev_on = False
                self.log("  context.dev: 3 failed searches in a row, switched off for this run")
            return None
        self.contextdev_fails = 0
        meta = raw.get("key_metadata") or {}
        used = meta.get("credits_consumed")
        if isinstance(used, int) and used != 1:
            db.begin_write(self.conn)
            state.record_usage(self.conn, CONTEXTDEV, used - 1)
            self.conn.commit()
            self.stats["contextdev_credits"] += used - 1
        if isinstance(meta.get("credits_remaining"), int) and meta["credits_remaining"] <= 0:
            self.contextdev_on = False
            self.log("  context.dev: no credits left, switched off")
        results = contextdev_results(raw)
        for r in results:
            text = re.sub(r"\s+", " ", r.pop("markdown")).strip()
            if text and r["url"] not in self.pages:
                self.pages[r["url"]] = {"url": r["url"], "text": text, "words": _padded(text)}
            hl = re.sub(r"\s+", " ", r.pop("highlights")).strip()
            if hl:          # used when we can't fetch the page ourselves (Facebook, ...)
                self.backup_text.setdefault(r["url"], hl)
        return results

    def search_searxng(self, query: str) -> list[dict] | None:
        """Results (maybe none), or None when SearXNG failed: an error, or no results
        with engines down. SEARXNG_MAX_FAILS in a row switch it off for the run."""
        db.begin_write(self.conn)
        state.record_usage(self.conn, SEARXNG)
        self.conn.commit()
        self.stats["searches"] += 1
        self.stats["searxng"] += 1
        try:
            resp = self.client.searxng(query)
            results, down = searxng_results(resp.json()) if resp.status == 200 else ([], [f"HTTP {resp.status}"])
        except (requests.RequestException, ValueError) as e:
            results, down = [], [type(e).__name__]
        if results or not down:
            self.searxng_fails = 0
            return results
        self.searxng_fails += 1
        self.log(f"  searxng: no results, down: {', '.join(down)}")
        if self.searxng_fails >= SEARXNG_MAX_FAILS:
            self.searxng_on = False
            self.log(f"  searxng: {SEARXNG_MAX_FAILS} failed searches in a row, switched off for this run")
        return None

    def search_firecrawl(self, query: str) -> list[dict]:
        db.begin_write(self.conn)
        state.record_usage(self.conn, SEARCH, SEARCH_CREDITS)
        self.conn.commit()
        self.stats["searches"] += 1
        self.stats["credits"] += SEARCH_CREDITS
        resp = self.call(lambda: self.client.search(query), "Firecrawl")
        if resp.status == 402:
            self.log(f"  firecrawl: HTTP 402 {resp.text[:300]}")
            raise Stop("Firecrawl HTTP 402 (credits spent)")
        if resp.status != 200:
            raise requests.RequestException(f"Firecrawl HTTP {resp.status} {resp.text[:200]}")
        raw = resp.json()
        used = raw.get("creditsUsed")
        if isinstance(used, int) and used != SEARCH_CREDITS:
            db.begin_write(self.conn)
            state.record_usage(self.conn, SEARCH, used - SEARCH_CREDITS)
            self.conn.commit()
            self.stats["credits"] += used - SEARCH_CREDITS
        return search_results(raw)

    def fetch(self, urls: list[str]) -> list[dict]:
        out = []
        for u in urls[:MAX_PAGES]:
            if u not in self.pages:
                self.stats["pages"] += 1
                try:
                    r = self.client.page(u)
                    text = page_text(r.text) if r.status == 200 else ""
                except requests.RequestException:
                    text = ""
                text = text or self.backup_text.get(u, "")
                self.pages[u] = {"url": u, "text": text, "words": _padded(text)}
            if self.pages[u]["text"]:
                out.append(self.pages[u])
        return out

    def ask(self, artist: str, shows: list[dict], pages: list[dict], snippets: list[dict]) -> dict | None:
        prompt = build_prompt(artist, shows, [{"url": p["url"], "excerpt": excerpts(p["text"], shows)}
                                              for p in pages], snippets)
        db.begin_write(self.conn)
        state.record_usage(self.conn, SOURCE)
        self.conn.commit()
        self.stats["gemini_calls"] += 1
        resp = self.call(lambda: self.client.generate(self.model, prompt), "Gemini")
        if resp.status != 200:
            raise requests.RequestException(f"Gemini HTTP {resp.status} {resp.text[:200]}")
        r = read_response(resp.json())
        self.stats["tokens"] += r["tokens"]
        answer = parse_answer(r["text"])
        if answer is None:
            self.log(f"  gemini: no JSON answer (finishReason {r['finish']})")
        return answer

    # one tour
    def tour(self, tour: list[dict]) -> None:
        artist = tour[0]["lead"]
        self.log(f"  tour: {artist}, {len(tour)} show(s) {tour[0]['date']}..{tour[-1]['date']}")
        snippets, urls = [], []
        for q in tour_queries(tour):
            for x in self.search(q):
                if x["url"] not in urls:
                    urls.append(x["url"])
                    snippets.append(x)
        pages = self.fetch(urls)
        texts = pages + [{"url": x["url"], "words": _padded(f"{x['title']} {x['description']}")}
                         for x in snippets]
        answer = (self.ask(artist, tour, pages, snippets) if snippets else None) or {"shows": {}, "other_shows": []}
        venues = [venue_core(s["venue"]) for s in tour]
        for s in tour:      # a residency: the venue alone doesn't say which night
            s["need_date"] = venues.count(venue_core(s["venue"])) > 1
        results = {s["id"]: self.judge(s, answer["shows"].get(s["date"]), texts) for s in tour}
        others = list(answer["other_shows"])
        all_texts = list(texts)

        fallbacks = 0
        for s in tour:
            if results[s["id"]]["specific"] or results[s["id"]]["festival"] or len(tour) == 1 \
                    or fallbacks >= MAX_FALLBACKS:
                continue
            if not self.can_afford(1):
                break
            fallbacks += 1
            res = self.search(show_query(s))
            if not res:
                continue
            new = [x for x in res if x["url"] not in urls]
            spages = self.fetch([x["url"] for x in res])
            stexts = spages + [{"url": x["url"], "words": _padded(f"{x['title']} {x['description']}")}
                               for x in res]
            a = self.ask(artist, [s], spages + [p for p in pages if p not in spages], res)
            if a is None:
                continue
            all_texts += stexts
            urls += [x["url"] for x in new]
            r = self.judge(s, a["shows"].get(s["date"]), stexts + texts)
            if r["specific"] or r["festival"] or (not results[s["id"]]["acts"] and r["acts"]):
                results[s["id"]] = r
            others += a["other_shows"]

        db.begin_write(self.conn)
        for s in tour:
            self.store(s, results[s["id"]])
        self.discover(tour, others, all_texts)
        self.conn.commit()

    def canonical(self, name: str) -> str:
        """A social-media handle the model copied ("@flatwormsband") as the graph
        artist it stands for ("Flat Worms"); other names unchanged."""
        n = name.strip().lstrip("@")
        if " " in n:
            return n
        key = norm(n).replace(" ", "")
        for k in [key] + [key[:-len(s)] for s in HANDLE_SUFFIXES if key.endswith(s) and len(key) > len(s) + 2]:
            row = self.conn.execute("SELECT name FROM artists WHERE in_graph = 1"
                                    " AND replace(name_norm, ' ', '') = ? LIMIT 1", (k,)).fetchone()
            if row:
                return row["name"]
        return n

    def judge(self, show: dict, ans: dict | None, texts: list[dict]) -> dict:
        """Which acts of an answer hold up against the texts, and how firmly."""
        out = {"specific": False, "acts": [], "quote": None, "url": None, "dropped": [], "festival": False}
        if not ans or not ans["openers"]:
            return out
        headliner = ans["headliner"] or show["lead"]
        acts = [(headliner, "headliner")] + [(o, "opener") for o in ans["openers"]
                                             if norm(o) != norm(headliner)]
        src = quote_source(ans["quote"], texts)
        qwords = _padded(ans["quote"] or "")
        specific = bool(src) and names_this_show(ans["quote"], show, [n for n, _ in acts]) \
            and all(mentions(qwords, o) for o, role in acts if role == "opener")
        show_pages = [t for t in texts if names_show(t["words"], show)]
        for pos, (name, role) in enumerate(acts):
            if specific and mentions(qwords, name):
                out["acts"].append((name, role, pos, "specific", src["url"]))
            else:
                # Named near the artist or the show's date, not just anywhere on a page
                # that also mentions the venue (a venue's calendar lists everyone).
                anchors = [show["lead"]] + date_forms(show["date"])
                page = next((t for t in show_pages if near(t["words"], name, anchors)), None)
                if page:
                    out["acts"].append((name, role, pos, "page", page["url"]))
                else:
                    out["dropped"].append(name)
        if not any(role == "opener" for _, role, *_ in out["acts"]):
            out["acts"] = []
        out["specific"] = specific
        out["festival"] = ans["festival"] or len(ans["openers"]) > MAX_OPENERS
        out["quote"] = ans["quote"] if specific else None
        out["url"] = src["url"] if specific else None
        return out

    def store(self, show: dict, r: dict) -> None:
        key = f"show:{show['id']}"
        self.stats["shows"] += 1
        if r["dropped"]:
            self.log(f"    {show['date']}: not on any fetched page, dropped: {', '.join(r['dropped'])}")
        if not r["acts"]:
            state.mark_checked(self.conn, SOURCE, key, changed=False,
                               min_days=NOT_FOUND_MIN_DAYS, max_days=NOT_FOUND_MAX_DAYS)
            self.stats["not_found"] += 1
            self.log(f"    {show['date']} {show['venue']}: nothing found")
            return
        for name, role, pos, check, url in r["acts"]:
            conf = CONF_SPECIFIC if check == "specific" else CONF_PAGE
            extra = {"model": self.model, "prompt_version": PROMPT_VERSION, "check": check,
                     **({"festival": True} if r["festival"] else {}),
                     **({"quote": r["quote"][:500]} if check == "specific" and r["quote"] else {})}
            aid = show["lead_id"] if norm(name) == norm(show["lead"]) else \
                ledger.get_or_create_artist(self.conn, self.canonical(name))
            ledger.add_claim(self.conn, show["id"], aid, SOURCE, key, role=role, confidence=conf,
                             position=pos, url=url, extra=extra, resolve=False)
        ledger.resolve_show(self.conn, show["id"])     # a festival flag on a claim marks the show
        if r["festival"]:   # a lineup: kept as a data point, not an opened-for; not asked again
            state.postpone(self.conn, SOURCE, key, DONE_DAYS)
            self.stats["festival"] += 1
        elif r["specific"]:
            state.postpone(self.conn, SOURCE, key, DONE_DAYS)
            self.stats["specific"] += 1
        else:   # tour-wide evidence only: kept, and asked again later
            state.mark_checked(self.conn, SOURCE, key, changed=False,
                               min_days=NOT_FOUND_MIN_DAYS, max_days=NOT_FOUND_MAX_DAYS)
            self.stats["page_only"] += 1
        self.log(f"    {show['date']} {show['venue']}: "
                 + ", ".join(f"{n} ({role}, {check})" for n, role, _, check, _ in r["acts"]))

    def discover(self, tour: list[dict], others: list[dict], texts: list[dict]) -> None:
        """Add the artist's shows that the pages list but the ledger lacks."""
        lead_id, artist = tour[0]["lead_id"], tour[0]["lead"]
        lo = date.fromisoformat(tour[0]["date"]) - timedelta(days=DISCOVER_DAYS)
        hi = date.fromisoformat(tour[-1]["date"]) + timedelta(days=DISCOVER_DAYS)
        countries = {s["country"] for s in tour if s.get("country")}
        seen = set()
        for o in others:
            try:
                d = date.fromisoformat(o["date"])
            except ValueError:
                continue
            if not lo <= d <= hi or (o["date"], norm(o["venue"])) in seen:
                continue
            if norm(o["venue"]) in (norm(o["city"]), norm(o["country"])):
                continue                                # a tour listing by city: no venue to file it under
            seen.add((o["date"], norm(o["venue"])))
            src = quote_source(o["quote"], texts)
            qwords = _padded(o["quote"])
            cand = {"date": o["date"], "venue": o["venue"], "city": o["city"]}
            if not src or not mentions(qwords, o["venue"]) or not any(
                    f" {k} " in qwords for k in show_keys(cand, numeric=True, city=False) if k != norm(o["venue"])):
                self.log(f"    other show {o['date']} {o['venue']}: quote not verified, skipped")
                continue
            names = [artist, o["headliner"] or ""] + o["openers"]
            if not date_confirmed(o["quote"], o["date"], names) and (
                    date_contradicted(o["quote"], o["date"], names)
                    or not year_from_context(src["words"], o["quote"], o["date"])):
                # A listing without the year (in it, or above it on the page) may be any year's tour.
                self.log(f"    other show {o['date']} {o['venue']}: year not pinned down, skipped ({o['quote'][:60]!r})")
                continue
            # The artist named in the quote, or at the top of the page (a tour page's title).
            if not (mentions(qwords, artist) or mentions(_padded(src.get("text", "")[:300]), artist)
                    or ("text" not in src and mentions(src["words"], artist))):
                continue
            # Known already: the artist on that date anywhere, or a day off in the same city
            # (some sources date a late show to the next day).
            near = self.conn.execute(
                "SELECT 1 FROM shows s JOIN claims c ON c.show_id = s.id JOIN venues v ON v.id = s.venue_id"
                " WHERE c.artist_id = ? AND (s.date = ? OR (s.date BETWEEN ? AND ? AND v.city_norm = ?)) LIMIT 1",
                (lead_id, o["date"], (d - timedelta(days=1)).isoformat(), (d + timedelta(days=1)).isoformat(),
                 norm(o["city"]))).fetchone()
            if near:
                continue                                # already known (maybe under another venue name)
            country = o["country"] or (next(iter(countries)) if len(countries) == 1 else None)
            vid = ledger.get_or_create_venue(self.conn, o["venue"], o["city"], country=country)
            native = f"found:{lead_id}:{o['date']}"
            headliner = o["headliner"] or artist
            acts = [(headliner, "headliner")] + [(x, "opener") for x in o["openers"] if norm(x) != norm(headliner)]
            if not any(norm(n) == norm(artist) for n, _ in acts):
                acts.append((artist, "unknown"))
            show_id = ledger.find_or_create_show(self.conn, o["date"], vid, {lead_id}, SOURCE, native)
            stored = []
            for pos, (name, role) in enumerate(acts):
                in_quote = mentions(qwords, name)
                if not (in_quote or norm(name) == norm(artist)):
                    continue                            # an act the quote doesn't name: not trusted
                aid = lead_id if norm(name) == norm(artist) else \
                    ledger.get_or_create_artist(self.conn, self.canonical(name))
                ledger.add_claim(self.conn, show_id, aid, SOURCE, native, role=role,
                                 confidence=CONF_SPECIFIC if in_quote else CONF_PAGE,
                                 position=pos if role != "unknown" else None, url=src["url"],
                                 extra={"model": self.model, "prompt_version": PROMPT_VERSION,
                                        "check": "discovered", "quote": o["quote"][:500],
                                        **({"festival": True} if o["festival"] or len(o["openers"]) > MAX_OPENERS
                                           else {})},
                                 resolve=False)
                stored.append(f"{name} ({role})")
            ledger.resolve_show(self.conn, show_id)
            self.stats["discovered"] += 1
            self.log(f"    NEW show {o['date']} {o['venue']}, {o['city'] or '?'}: {', '.join(stored)}")


def ask(conn: sqlite3.Connection, client: Client, max_requests: int = DAILY_LIMIT,
        max_credits: int | None = None, monthly_credits: int | None = MONTHLY_CREDITS,
        max_acts: int = MAX_ACTS, log=print) -> dict:
    """Spend search credits and Gemini calls on due shows, a tour at a time, until a
    provider's quota (or a cap) is reached or nothing is due.
    Commits before every network request; holds the write lock only to store (see
    "Concurrency" in docs/ARCHITECTURE.md)."""
    run = Run(conn, client, max_requests, max_credits, monthly_credits, max_acts, log)
    stats = run.stats
    db.begin_write(conn)
    queue_shows(conn, max_acts)
    conn.commit()

    handled: set[int] = set()
    errors = 0
    for row in state.due(conn, SOURCE, limit=100_000):
        show_id = int(row["key"].split(":", 1)[1])
        if show_id in handled:
            continue
        show = _show(conn, show_id)
        if not _qualifies(show, max_acts):
            # Another source filled it in (or max_acts was lowered). Forgotten rather than
            # postponed, so queue_shows brings it back if it qualifies again.
            db.begin_write(conn)
            state.forget(conn, SOURCE, row["key"])
            conn.commit()
            stats["skipped"] += 1
            continue
        tour = tour_of(conn, show, max_acts, handled)
        if not run.can_afford(len(tour_queries(tour))):
            stats["stopped"] = "budget spent"
            break
        handled.update(s["id"] for s in tour)
        stats["tours"] += 1
        try:
            run.tour(tour)
            errors = 0
        except Stop as e:
            conn.rollback()
            stats["stopped"] = str(e)
            break
        except (requests.RequestException, ValueError) as e:
            conn.rollback()
            log(f"  {show['lead']} tour: {type(e).__name__} {str(e)[:200]}")
            db.begin_write(conn)
            for s in tour:
                state.mark_failed(conn, SOURCE, f"show:{s['id']}")
            conn.commit()
            stats["errors"] += 1
            errors += 1
            if errors >= MAX_ERRORS:
                stats["stopped"] = f"{MAX_ERRORS} errors in a row"
                break
    return stats
