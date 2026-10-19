import json
from datetime import datetime, timezone

import requests

from concertdb import ledger, seed, state
from concertdb.http import Response
from concertdb.sources import gemini
from conftest import add_graph_artist, ok, status

TY = "07a17571-81fc-4cf8-a634-98f0d926d313"
OTHER = "11111111-2222-3333-4444-555555555555"
QUIET = dict(log=lambda *_: None)

# A promoter's tour page (hand-written in the shape of the real one): every date with
# its own support acts, including two dates the ledger doesn't have yet.
TOUR_URL = "https://promoter.example/news/ty-segall/"
TOUR_PAGE = """<html><head><title>Ty Segall Australian Tour 2026</title>
<script>var x = "Fake Band at the Metro";</script></head><body>
<h1>TY SEGALL</h1><p>Australian tour, February / March 2026. Earth Tongue joins Ty on all dates.</p>
<p>WED 18 FEB &ndash; BYRON BAY</p>
<p>THU 26 FEB &ndash; MO&#8217;S DESERT CLUBHOUSE, GOLD COAST w/ Earth Tongue, Speeding Vehicle</p>
<p>FRI 27 FEB &ndash; PRINCESS THEATRE, BRISBANE w/ Earth Tongue, Knee</p>
<p>SAT 28 FEB &ndash; DAZE AWAY @ A &amp; I HALL, BANGALOW w/ Earth Tongue, Media Puzzle</p>
<p>MON 2 MAR &ndash; THE METRO, SYDNEY</p>
<p>THU 10 DEC &ndash; FAR AWAY HALL, PERTH w/ Someone Else</p>
</body></html>"""
METRO_URL = "https://venue.example/metro/ty-segall"
METRO_PAGE = ("<html><body><h2>Ty Segall</h2><p>Monday 2 March 2026, The Metro Theatre, Sydney."
              " Support from Earth Tongue and R.M.F.C.</p></body></html>")


def web(*results):
    return ok(json.dumps({"success": True, "creditsUsed": 2, "data": {"web": [
        {"url": u, "title": t, "description": d} for u, t, d in results]}}))


def answer(obj, tokens=1000):
    return ok(json.dumps({"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}}],
                          "usageMetadata": {"totalTokenCount": tokens}}))


class FakeClient:
    model = "gemini-test"

    def __init__(self, answers, searches=None, pages=None):
        self.answers = list(answers)
        self.searches = searches or {}      # substring of the query -> response
        self.pages = pages or {}
        self.prompts, self.queries, self.fetched = [], [], []

    def search(self, query):
        self.queries.append(query)
        for k, v in self.searches.items():
            if k in query:
                if isinstance(v, list):
                    v = v.pop(0) if len(v) > 1 else v[0]
                if isinstance(v, Exception):
                    raise v
                return v
        return web()

    def generate(self, model, prompt):
        self.prompts.append(prompt)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    def page(self, url):
        self.fetched.append(url)
        text = self.pages.get(url)
        return Response(200, text, None, None, None, url=url) if text else status(404)


def ty_show(conn, day, venue, city, mbid=TY, name="Ty Segall"):
    if not conn.execute("SELECT 1 FROM artists WHERE id = ?", (mbid,)).fetchone():
        add_graph_artist(conn, mbid, name)
    v = ledger.get_or_create_venue(conn, venue, city, country="Australia")
    show = ledger.find_or_create_show(conn, day, v)
    ledger.add_claim(conn, show, mbid, "setlistfm", f"s-{mbid}-{day}")
    conn.commit()
    return show


def ty_tour(conn):
    return [ty_show(conn, "2026-02-26", "Mo's Desert Clubhouse", "Gold Coast"),
            ty_show(conn, "2026-02-27", "Princess Theatre", "Brisbane"),
            ty_show(conn, "2026-03-02", "The Metro", "Sydney")]


def billing(conn, show):
    return {r["name"]: (r["role"], r["confidence"]) for r in conn.execute(
        "SELECT a.name, b.role, b.confidence FROM billings b JOIN artists a ON a.id = b.artist_id"
        " WHERE b.show_id = ?", (show,))}


TOUR_ANSWER = {
    "shows": [
        {"date": "2026-02-26", "headliner": "Ty Segall", "openers": ["Earth Tongue", "Speeding Vehicle"],
         "show_specific_quote": "THU 26 FEB – MO'S DESERT CLUBHOUSE, GOLD COAST w/ Earth Tongue, Speeding Vehicle",
         "url": TOUR_URL},
        {"date": "2026-02-27", "headliner": "Ty Segall", "openers": ["Earth Tongue", "Knee"],
         "show_specific_quote": "FRI 27 FEB – PRINCESS THEATRE, BRISBANE w/ Earth Tongue, Knee", "url": TOUR_URL},
        # Only the tour-wide line: not done, gets its own search.
        {"date": "2026-03-02", "headliner": "Ty Segall", "openers": ["Earth Tongue"],
         "show_specific_quote": None, "url": TOUR_URL},
    ],
    "other_shows": [
        {"date": "2026-02-28", "venue": "A & I Hall", "city": "Bangalow", "country": "Australia",
         "headliner": "Ty Segall", "openers": ["Earth Tongue", "Media Puzzle"],
         "quote": "SAT 28 FEB – DAZE AWAY @ A & I HALL, BANGALOW w/ Earth Tongue, Media Puzzle", "url": TOUR_URL},
        {"date": "2026-03-01", "venue": "Made Up Club", "city": "Wollongong", "country": "Australia",
         "headliner": "Ty Segall", "openers": ["Nobody"], "quote": "SUN 1 MAR - MADE UP CLUB w/ Nobody",
         "url": TOUR_URL},                                            # not on any page
        {"date": "2026-02-18", "venue": "Byron Bay", "city": "Byron Bay", "country": "Australia",
         "headliner": "Ty Segall", "openers": [], "quote": "WED 18 FEB \u2013 BYRON BAY", "url": TOUR_URL},     # a city, not a venue
        {"date": "2026-12-10", "venue": "Far Away Hall", "city": "Perth", "country": "Australia",
         "headliner": "Ty Segall", "openers": ["Someone Else"],
         "quote": "THU 10 DEC – FAR AWAY HALL, PERTH w/ Someone Else", "url": TOUR_URL},   # another tour
    ],
}
METRO_ANSWER = {"shows": [{"date": "2026-03-02", "headliner": "Ty Segall",
                           "openers": ["Earth Tongue", "R.M.F.C."],
                           "show_specific_quote": "Monday 2 March 2026, The Metro Theatre, Sydney. Support from Earth Tongue and R.M.F.C.",
                           "url": METRO_URL}], "other_shows": []}


def ty_client():
    return FakeClient(
        [answer(TOUR_ANSWER), answer(METRO_ANSWER)],
        searches={"tour support": web((TOUR_URL, "Ty Segall Australian Tour", "Earth Tongue on all dates")),
                  '"The Metro" Sydney': web((METRO_URL, "Ty Segall - The Metro", "Monday 2 March"))},
        pages={TOUR_URL: TOUR_PAGE, METRO_URL: METRO_PAGE})


# ------------------------------------------------------------------ the pipeline

def test_a_tour_in_one_gemini_call_plus_one_fallback(conn):
    gold, bris, metro = ty_tour(conn)
    client = ty_client()
    stats = gemini.ask(conn, client, **QUIET)
    # Tour + venue searches, then one show search for the Metro (2 credits each).
    assert len(client.queries) == 3 and stats["credits"] == 6 and stats["gemini_calls"] == 2
    assert client.queries[0] == "Ty Segall Australia 2026 tour support"
    assert client.queries[1] == 'Ty Segall "Mo\'s Desert Clubhouse" "Princess Theatre" "The Metro" 2026'
    assert client.queries[2].startswith('"The Metro" Sydney Ty Segall March 2 2026')
    assert state.requests_today(conn, "firecrawl") == 6 and state.requests_today(conn, "gemini") == 2
    assert client.fetched.count(TOUR_URL) == 1                       # fetched once, reused
    assert stats["specific"] == 3 and stats["tours"] == 1

    assert billing(conn, gold)["Speeding Vehicle"][0] == "opener"
    b = billing(conn, metro)
    assert set(b) == {"Ty Segall", "Earth Tongue", "R.M.F.C."}
    assert b["R.M.F.C."][0] == "opener"
    c = conn.execute("SELECT url, confidence, extra FROM claims c JOIN artists a ON a.id = c.artist_id"
                     " WHERE c.source = 'gemini' AND c.show_id = ? AND a.name = 'Knee'", (bris,)).fetchone()
    assert c["url"] == TOUR_URL and c["confidence"] == gemini.CONF_SPECIFIC
    assert json.loads(c["extra"])["check"] == "specific" and "BRISBANE" in json.loads(c["extra"])["quote"]
    # Done: not asked again.
    assert gemini.ask(conn, FakeClient([]), **QUIET)["gemini_calls"] == 0


def test_shows_the_pages_list_are_added(conn):
    ty_tour(conn)
    stats = gemini.ask(conn, ty_client(), **QUIET)
    assert stats["discovered"] == 1         # not the unquoted one, not a city as venue, not one months later
    s = conn.execute("SELECT s.id, s.date, v.name, v.city, v.country FROM shows s JOIN venues v ON v.id = s.venue_id"
                     " WHERE s.date = '2026-02-28'").fetchone()
    assert (s["name"], s["city"], s["country"]) == ("A & I Hall", "Bangalow", "Australia")
    b = billing(conn, s["id"])
    assert b["Ty Segall"][0] == "headliner" and b["Media Puzzle"][0] == "opener"
    assert conn.execute("SELECT artist_id FROM claims WHERE show_id = ? AND artist_id = ?",
                        (s["id"], TY)).fetchone()           # the graph artist, not a name:... duplicate
    assert not conn.execute("SELECT 1 FROM shows WHERE date IN ('2026-02-18', '2026-03-01', '2026-12-10')").fetchone()
    # Asked again later: the same show isn't added twice.
    conn.execute("UPDATE crawl_state SET next_due = '2000-01-01' WHERE source = 'gemini'")
    conn.commit()
    gemini.ask(conn, ty_client(), **QUIET)
    assert conn.execute("SELECT COUNT(*) FROM shows WHERE date = '2026-02-28'").fetchone()[0] == 1


def test_a_known_show_is_not_discovered_again(conn):
    ty_tour(conn)
    ty_show(conn, "2026-02-28", "A&I Hall", "Bangalow")         # known, written differently
    stats = gemini.ask(conn, ty_client(), **QUIET)
    assert stats["discovered"] == 0
    assert conn.execute("SELECT COUNT(*) FROM shows WHERE date = '2026-02-28'").fetchone()[0] == 1


def test_tour_wide_only_is_kept_weaker_and_asked_again(conn):
    ty_tour(conn)
    client = ty_client()
    client.answers[1] = answer({"shows": [], "other_shows": []})     # the Metro search finds nothing
    stats = gemini.ask(conn, client, **QUIET)
    assert stats["specific"] == 2 and stats["page_only"] == 1
    c = conn.execute("SELECT confidence, extra FROM claims c JOIN artists a ON a.id = c.artist_id"
                     " WHERE c.source = 'gemini' AND c.show_id = 3 AND a.name = 'Earth Tongue'").fetchone()
    assert c["confidence"] == gemini.CONF_PAGE and json.loads(c["extra"])["check"] == "page"
    assert state.get(conn, "gemini", "show:3")["interval_days"] == gemini.NOT_FOUND_MIN_DAYS


def test_acts_on_no_fetched_page_are_dropped(conn):
    show = ty_show(conn, "2026-03-02", "The Metro", "Sydney")
    client = FakeClient(
        [answer({"shows": [{"date": "2026-03-02", "headliner": "Ty Segall", "openers": ["Made Up Band"],
                            "show_specific_quote": "The Metro, Sydney with Made Up Band", "url": METRO_URL}]})],
        searches={"Metro": web((METRO_URL, "Ty Segall", ""))}, pages={METRO_URL: METRO_PAGE})
    stats = gemini.ask(conn, client, **QUIET)
    assert stats["not_found"] == 1 and len(client.queries) == 1     # one show: one search, no fallback
    assert not conn.execute("SELECT 1 FROM claims WHERE source = 'gemini'").fetchone()
    assert set(billing(conn, show)) == {"Ty Segall"}


def test_a_residency_needs_the_date_in_the_quote(conn):
    first = ty_show(conn, "2026-03-02", "The Metro", "Sydney")
    second = ty_show(conn, "2026-03-03", "The Metro", "Sydney")
    page = "<p>Ty Segall at The Metro, Sydney with Earth Tongue. 2 March and 3 March.</p>"
    quote = "Ty Segall at The Metro, Sydney with Earth Tongue."
    a = {"shows": [{"date": d, "headliner": "Ty Segall", "openers": ["Earth Tongue"],
                    "show_specific_quote": quote, "url": TOUR_URL} for d in ("2026-03-02", "2026-03-03")]}
    client = FakeClient([answer(a), answer({"shows": []}), answer({"shows": []})],
                        searches={"Ty Segall": web((TOUR_URL, "Ty Segall", ""))}, pages={TOUR_URL: page})
    stats = gemini.ask(conn, client, **QUIET)
    assert stats["specific"] == 0 and stats["page_only"] == 2
    assert billing(conn, first)["Earth Tongue"][0] == billing(conn, second)["Earth Tongue"][0] == "opener"


def test_quote_must_be_on_a_page_and_may_skip_text():
    texts = [{"url": "u", "words": gemini._padded(gemini.page_text(TOUR_PAGE))}]
    assert gemini.quote_source("FRI 27 FEB - Princess Theatre, Brisbane w/ Earth Tongue, Knee", texts)
    assert gemini.quote_source("THU 26 FEB ... FRI 27 FEB … Knee", texts)
    assert not gemini.quote_source("Knee ... THU 26 FEB", texts)            # out of order
    assert not gemini.quote_source("FRI 27 FEB - Princess Theatre w/ Fake Band", texts)
    assert not gemini.quote_source("Fake Band at the Metro", texts)          # script text isn't page text
    assert not gemini.quote_source(None, texts)


def test_parse_answer():
    a = gemini.parse_answer("```json\n" + json.dumps(TOUR_ANSWER) + "\n```")
    assert a["shows"]["2026-02-27"]["openers"] == ["Earth Tongue", "Knee"]
    assert a["shows"]["2026-03-02"]["quote"] is None and len(a["other_shows"]) == 4
    assert gemini.parse_answer("Sorry, no.") is None
    assert gemini.parse_answer('{"shows": "x"}') is None
    assert gemini.parse_answer('{"shows": [{"date": "2026-01-01", "openers": "A"}]}')["shows"]["2026-01-01"]["openers"] == []


def test_prompt_lists_the_shows_and_only_text_near_them():
    shows = [{"date": "2026-02-27", "venue": "Princess Theatre", "city": "Brisbane", "country": "Australia"}]
    text = "unrelated " * 300 + "FRI 27 FEB - PRINCESS THEATRE w/ Knee" + " unrelated" * 300
    p = gemini.build_prompt("Ty Segall", shows, [{"url": "u", "excerpt": gemini.excerpts(text, shows)}], [])
    assert "- 2026-02-27 (Friday): Princess Theatre, Brisbane, Australia" in p
    assert "w/ Knee" in p and p.count("unrelated") < 100


# ------------------------------------------------------------------ queue and tours

def test_tours_group_nearby_shows(conn):
    ids = ty_tour(conn) + [ty_show(conn, "2026-06-01", "Elsewhere", "London")]
    gemini.queue_shows(conn)
    tour = gemini.tour_of(conn, gemini._show(conn, ids[1]), 1, set())
    assert [s["id"] for s in tour] == ids[:3]
    assert [s["id"] for s in gemini.tour_of(conn, gemini._show(conn, ids[3]), 1, set())] == [ids[3]]
    assert gemini.tour_queries(gemini.tour_of(conn, gemini._show(conn, ids[3]), 1, set())) == [
        '"Elsewhere" London Ty Segall June 1 2026 support']


def test_tour_size_is_capped(conn, monkeypatch):
    monkeypatch.setattr(gemini, "TOUR_MAX_SHOWS", 3)
    ids = [ty_show(conn, f"2026-03-{d:02d}", f"Venue {d}", "City") for d in range(1, 11)]
    gemini.queue_shows(conn)
    tour = gemini.tour_of(conn, gemini._show(conn, ids[5]), 1, set())
    assert len(tour) == 3 and ids[5] in [s["id"] for s in tour]


def test_prioritized_artists_then_recent_shows_first(conn):
    old = ty_show(conn, "2010-05-01", "Old Hall", "Paris", OTHER, "Pigface")
    recent = ty_show(conn, "2026-05-01", "New Hall", "Paris", OTHER, "Pigface")
    mine = ty_show(conn, "2015-05-01", "Small Club", "Oslo")
    gemini.queue_shows(conn)
    assert [r["key"] for r in state.due(conn, "gemini")] == [f"show:{recent}", f"show:{mine}", f"show:{old}"]
    state.ensure(conn, "setlistfm", f"artist:{TY}", priority=seed.USER_PRIORITY)
    gemini.queue_shows(conn)
    assert state.due(conn, "gemini")[0]["key"] == f"show:{mine}"


def test_tours_run_in_queue_order(conn):
    ty_show(conn, "2026-05-01", "Hall A", "Paris", OTHER, "Pigface")
    ty_show(conn, "2025-05-01", "Hall B", "Paris", OTHER, "Pigface")    # a different tour
    ty_show(conn, "2024-05-01", "Small Club", "Oslo")
    empty = answer({"shows": [], "other_shows": []})
    client = FakeClient([empty, empty, empty], searches={
        "Hall": web(("https://x.example/a", "Pigface", "")),
        "Small Club": web(("https://x.example/b", "Ty Segall", ""))})
    stats = gemini.ask(conn, client, **QUIET)
    assert stats["tours"] == 3 and len(client.prompts) == 3
    assert ["Hall A" in client.prompts[0], "Hall B" in client.prompts[1], "Ty Segall" in client.prompts[2]] == [True] * 3


def test_only_single_act_past_shows_are_queued(conn):
    show = ty_show(conn, "2026-03-02", "The Metro", "Sydney")
    v = ledger.get_or_create_venue(conn, "The Metro", "Sydney")
    full = ledger.find_or_create_show(conn, "2026-03-05", v)
    ledger.add_claim(conn, full, TY, "manual", "m1", role="headliner", confidence=1.0)
    ledger.add_claim(conn, full, ledger.get_or_create_artist(conn, "Earth Tongue"), "manual", "m1",
                     role="opener", confidence=1.0)
    ledger.add_claim(conn, ledger.find_or_create_show(conn, "2099-01-01", v), TY, "manual", "m2")   # upcoming
    nobody = ledger.get_or_create_venue(conn, "Elsewhere", "Nowhere")       # no graph artist
    ledger.add_claim(conn, ledger.find_or_create_show(conn, "2026-01-01", nobody),
                     ledger.get_or_create_artist(conn, "Unknown Band"), "setlistfm", "s9")
    assert gemini.queue_shows(conn) == 1
    assert gemini.queue_shows(conn, max_acts=2) == 2
    assert state.get(conn, "gemini", f"show:{show}")


def test_skips_a_show_another_source_filled_in(conn):
    show = ty_show(conn, "2026-03-02", "The Metro", "Sydney")
    gemini.queue_shows(conn)
    ledger.add_claim(conn, show, ledger.get_or_create_artist(conn, "Earth Tongue"), "manual", "m1",
                     role="opener", confidence=1.0)
    conn.commit()
    client = FakeClient([])
    stats = gemini.ask(conn, client, **QUIET)
    assert stats["skipped"] == 1 and client.queries == []
    assert state.get(conn, "gemini", f"show:{show}") is None


# ------------------------------------------------------------------ budgets and failures

def test_no_credits_no_requests(conn):
    ty_tour(conn)
    client = ty_client()
    stats = gemini.ask(conn, client, max_credits=3, **QUIET)     # less than the tour's two searches
    assert stats["stopped"] == "budget spent" and client.queries == []
    stats = gemini.ask(conn, client, monthly_credits=0, **QUIET)
    assert client.queries == []


def test_out_of_firecrawl_credits_ends_the_run(conn):
    ty_tour(conn)
    client = FakeClient([], searches={"tour": Response(402, "Payment required", None, None, None)})
    stats = gemini.ask(conn, client, **QUIET)
    assert stats["stopped"].startswith("Firecrawl HTTP 402")
    assert len(state.due(conn, "gemini")) == 3


def test_gemini_daily_quota_ends_the_run_and_keeps_the_shows_due(conn):
    ty_tour(conn)
    client = ty_client()
    client.answers = [Response(429, '{"error": {"details": [{"quotaId": "GenerateRequestsPerDayPerProject"}]}}',
                               None, None, None)]
    stats = gemini.ask(conn, client, **QUIET)
    assert stats["stopped"].startswith("Gemini HTTP 429")
    assert len(state.due(conn, "gemini")) == 3


def test_per_minute_429s_are_waited_out(conn, monkeypatch):
    ty_tour(conn)
    client = ty_client()
    minute = Response(429, '{"error": {"details": [{"retryDelay": "20s"}]}}', None, None, None)
    client.answers.insert(0, minute)
    client.searches["tour support"] = [Response(429, "rate limited", None, None, None, retry_after=7.0),
                                       client.searches["tour support"]]
    waits = []
    monkeypatch.setattr(gemini.time, "sleep", waits.append)
    stats = gemini.ask(conn, client, **QUIET)
    assert waits == [7.0, 21.0] and stats["specific"] == 3 and stats["stopped"] is None


def test_endless_429s_end_the_run(conn, monkeypatch):
    ty_tour(conn)
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    client = FakeClient([], searches={"tour": Response(429, "", None, None, None)})
    stats = gemini.ask(conn, client, **QUIET)
    assert stats["stopped"].startswith("Firecrawl HTTP 429") and len(client.queries) == gemini.RETRIES_429 + 1


def test_gemini_daily_limit(conn, monkeypatch):
    monkeypatch.setattr(gemini, "DAILY_LIMIT", 1)
    ty_show(conn, "2026-03-02", "The Metro", "Sydney")
    ty_show(conn, "2025-03-02", "The Metro", "Sydney", OTHER, "Pigface")
    empty = answer({"shows": []})
    client = FakeClient([empty, empty], searches={"Metro": web((METRO_URL, "Ty Segall", ""))})
    stats = gemini.ask(conn, client, **QUIET)
    assert len(client.prompts) == 1 and stats["stopped"] == "budget spent"


def test_errors_back_off(conn):
    ty_tour(conn)
    stats = gemini.ask(conn, FakeClient([], searches={"tour": requests.ConnectionError()}), **QUIET)
    assert stats["errors"] == 1 and state.due(conn, "gemini") == []
    assert all(state.get(conn, "gemini", f"show:{i}")["failures"] == 1 for i in (1, 2, 3))


def test_gemini_quota_day_is_pacific():
    # 03:00 UTC on Oct 18 is still Oct 17 in Los Angeles, where the free-tier quota resets.
    t = datetime(2026, 10, 18, 3, 0, tzinfo=timezone.utc)
    assert state.usage_day("gemini", t).isoformat() == "2026-10-17"
    assert state.usage_day("firecrawl", t).isoformat() == "2026-10-18"


def test_no_transaction_is_open_during_network_calls(conn):
    # Another process (run, pollstar) must be able to write while this waits on the network.
    ty_tour(conn)
    seen = []

    class Watching(FakeClient):
        def search(self, q):
            seen.append(conn.in_transaction)
            return super().search(q)

        def generate(self, model, prompt):
            seen.append(conn.in_transaction)
            return super().generate(model, prompt)

        def page(self, url):
            seen.append(conn.in_transaction)
            return super().page(url)

    c = ty_client()
    client = Watching(c.answers, c.searches, c.pages)
    assert gemini.ask(conn, client, **QUIET)["specific"] == 3
    assert len(seen) >= 6 and not any(seen)
    assert not conn.in_transaction


def test_pages_are_not_archived_but_searches_and_answers_are(tmp_path):
    import gzip
    from concertdb.http import Fetcher

    class FakeSession:
        headers = {}

        def get(self, url, params=None, headers=None, timeout=None):
            class R:
                status_code, headers, text = 200, {}, TOUR_PAGE
            R.url = url
            return R()

        def post(self, url, json=None, headers=None, timeout=None):
            class R:
                status_code, headers, text = 200, {}, '{"data": {"web": []}, "creditsUsed": 2}'
            R.url = url
            return R()

    f = Fetcher(tmp_path, session=FakeSession())
    client = gemini.Client(f, f, f, "g-key", "fc-key")
    assert client.model == gemini.MODEL
    client.page(TOUR_URL)
    client.search("Ty Segall tour")
    assert not (tmp_path / "raw" / gemini.PAGES).exists()
    archived = gzip.open(next((tmp_path / "raw" / gemini.SEARCH).glob("*.jsonl.gz")), "rt").read()
    assert "Ty Segall tour" in archived and "fc-key" not in archived


# ------------------------------------------------------------------ quality rules

def test_a_show_specific_quote_names_this_show():
    exit_in = {"date": "2024-10-28", "venue": "Exit/In", "city": "Nashville"}
    assert not gemini.names_this_show("10/28/26 @ Variety Playhouse Atlanta, Georgia w/ Traps PS", exit_in)
    assert gemini.names_this_show("Osees at Exit/In w/ Traps PS", exit_in)
    assert gemini.names_this_show("Mon Oct 28 - Nashville, TN w/ Traps PS", exit_in)          # date + city
    assert not gemini.names_this_show("Nashville w/ Traps PS", exit_in)                       # city alone
    assert not gemini.names_this_show("Oct 28, 2023 Exit/In w/ Traps PS", exit_in)           # another year
    assert not gemini.names_this_show("Tue. Oct 28 - Exit/In w/ Traps PS", exit_in)          # another weekday
    byron = {"date": "2016-01-10", "venue": "Great Northern Hotel", "city": "Byron Bay"}
    assert not gemini.names_this_show("Throughout their 11 date tour (Byron Bay, Sydney, Melbourne)"
                                      " they will play alongside Blank Realm", byron)


def test_a_new_show_needs_its_year_pinned_down():
    assert not gemini.date_confirmed("June 9 – New Century – Manchester", "2024-06-09")
    assert not gemini.date_confirmed("Sun. October 18 – Cleveland, OH @ Beachland Ballroom", "2022-10-18")
    assert gemini.date_confirmed("Sun. October 18 – Cleveland, OH @ Beachland Ballroom", "2020-10-18")
    assert gemini.date_confirmed("May 28, 2018 The Earl · East Atlanta", "2018-05-28")
    assert gemini.date_confirmed("by Old Psyche Man on 9/17/24 191 Toole - Tucson", "2024-09-17")
    assert not gemini.date_confirmed("by Old Psyche Man on 9/17/24 191 Toole - Tucson", "2025-09-17")


def test_festival_lineups_are_kept_flagged_and_not_exported(conn):
    from concertdb import export
    show = ty_show(conn, "2017-07-21", "Exposition Park", "Los Angeles")
    lineup = {"shows": [{"date": "2017-07-21", "festival": True, "headliner": "Missy Elliott",
                         "openers": ["Bjork", "Ty Segall"], "url": "https://fyf.example",
                         "show_specific_quote": "FYF Fest, Exposition Park, July 21 2017: Missy Elliott, Bjork, Ty Segall"}]}
    client = FakeClient([answer(lineup)], searches={"Exposition": web(
        ("https://fyf.example", "FYF Fest 2017",
         "FYF Fest, Exposition Park, July 21 2017: Missy Elliott, Bjork, Ty Segall"))})
    stats = gemini.ask(conn, client, **QUIET)
    assert stats["festival"] == 1 and len(client.prompts) == 1
    assert set(billing(conn, show)) == {"Missy Elliott", "Bjork", "Ty Segall"}
    assert all(json.loads(r["extra"])["festival"] for r in conn.execute(
        "SELECT extra FROM claims WHERE source = 'gemini'"))
    assert conn.execute("SELECT is_festival FROM shows WHERE id = ?", (show,)).fetchone()[0] == 1
    assert export.opened_for(conn, min_conf=0) == []
    assert state.get(conn, "gemini", f"show:{show}")["interval_days"] == gemini.DONE_DAYS
    many = {"date": "2017-07-21", "festival": False, "headliner": "X", "openers": list("ABCDEFG"),
            "quote": None, "url": None}
    assert gemini.Run(conn, client, 1, None, None, 1, print).judge(gemini._show(conn, show), many, [])["festival"]


def test_undated_listings_are_not_added(conn):
    ty_tour(conn)
    client = ty_client()
    a = json.loads(json.dumps(TOUR_ANSWER))
    a["other_shows"][0]["quote"] = "28 FEB – DAZE AWAY @ A & I HALL, BANGALOW w/ Earth Tongue, Media Puzzle"
    client.answers[0] = answer(a)
    client.pages[TOUR_URL] = TOUR_PAGE.replace("SAT 28 FEB", "28 FEB")
    assert gemini.ask(conn, client, **QUIET)["discovered"] == 0


def test_overloaded_gemini_is_waited_out(conn, monkeypatch):
    ty_tour(conn)
    client = ty_client()
    client.answers.insert(0, Response(503, "overloaded", None, None, None))
    waits = []
    monkeypatch.setattr(gemini.time, "sleep", waits.append)
    stats = gemini.ask(conn, client, **QUIET)
    assert waits == [gemini.TRANSIENT_WAIT] and stats["specific"] == 3 and stats["errors"] == 0


def test_a_band_named_after_a_weekday_is_not_a_date():
    show = {"date": "2024-10-28", "venue": "Exit/In", "city": "Nashville"}
    assert gemini.names_this_show("Exit/In w/ Wednesday", show, ["Wednesday"])
    assert not gemini.names_this_show("Exit/In w/ Wednesday", show)


def test_quotes_may_shorten_the_venue_or_pin_the_date():
    assert gemini.venue_keys("Le Transbordeur") == ["le transbordeur", "transbordeur"]
    assert "teragram" in gemini.venue_keys("Teragram Ballroom")
    assert gemini.venue_keys("Hall") == ["hall"]
    assert gemini.names_this_show("[complet] OSEES + OVER-GAIN OPTIMAL DEATH au Transbordeur — Sunday, July 3, 2022",
                                  {"date": "2022-07-03", "venue": "Le Transbordeur", "city": "Villeurbanne"})
    assert gemini.names_this_show("VOLUME 3 with support from Coral Web [November 29, 2025 / Doors 8pm",
                                  {"date": "2025-11-29", "venue": "Teragram Ballroom", "city": "Los Angeles"})
    assert gemini.names_this_show("Fri, Sept 16 – Thalia Hall * Sat, Sept 17 – Thalia Hall * with Bronze",
                                  {"date": "2022-09-17", "venue": "Thalia Hall", "city": "Chicago", "need_date": True})
    assert not gemini.names_this_show("Joining Osees on May 16th for a night of psychedelic rock",
                                      {"date": "2025-05-16", "venue": "Podium Kruidtuin", "city": "Brussels"})
