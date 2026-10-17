import json
from datetime import datetime, timezone

import requests

from concertdb import ledger, state
from concertdb.http import Response
from concertdb.sources import gemini
from conftest import FLAT_WORMS, add_graph_artist, fixture_text, ok, status

IDC = "11111111-2222-3333-4444-555555555555"   # stand-in MBID for Iguana Death Cult
CAPTURED = ok(fixture_text("gemini_generate_zebulon.json"))
GROUNDED = ok(fixture_text("gemini_generate_grounded.json"))
EVENT_PAGE = fixture_text("event_page.html")
QUIET = dict(log=lambda *_: None)


class FakeClient:
    def __init__(self, answers, pages=None, model="gemini-3.8-flash"):
        self.answers = list(answers)
        self.pages = pages or {}
        self.model = model
        self.prompts = []
        self.fetched = []

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


def single_act_show(conn, date="2026-09-17"):
    if not conn.execute("SELECT 1 FROM artists WHERE id = ?", (IDC,)).fetchone():
        add_graph_artist(conn, IDC, "Iguana Death Cult")
        add_graph_artist(conn, FLAT_WORMS, "Flat Worms")
    v = ledger.get_or_create_venue(conn, "Zebulon", "Los Angeles")
    show = ledger.find_or_create_show(conn, date, v)
    ledger.add_claim(conn, show, IDC, "setlistfm", "s1", role="unknown", confidence=0.0)
    conn.commit()
    return show


def billing(conn, show):
    return {r["artist_id"]: (r["role"], r["confidence"])
            for r in conn.execute("SELECT * FROM billings WHERE show_id = ?", (show,))}


def test_pick_model():
    models = json.loads(fixture_text("gemini_models.json"))["models"]
    assert gemini.pick_model(models) == "gemini-3.8-flash"
    assert gemini.pick_model(models[:1]) == "gemini-2.5-flash"
    assert gemini.pick_model([{"name": "models/gemini-3.10-flash",
                               "supportedGenerationMethods": ["generateContent"]}] + models) == "gemini-3.10-flash"


def test_parse_captured_answer():
    r = gemini.read_response(json.loads(fixture_text("gemini_generate_zebulon.json")))
    assert r["sources"] == [] and r["queries"] == []        # this real response had no grounding metadata
    a = gemini.parse_answer(r["text"])
    assert a["headliner"] == "Iguana Death Cult" and a["openers"] == ["Flat Worms"] and a["confident"]
    assert a["evidence"][0]["url"].startswith("https://vertexaisearch.cloud.google.com/")
    assert gemini.parse_answer("Sorry, I couldn't find it.") is None
    assert gemini.parse_answer('{"openers": "Flat Worms"}') is None


def test_thoughts_are_not_part_of_the_answer():
    r = gemini.read_response(json.loads(fixture_text("gemini_generate_grounded.json")))
    assert r["text"].startswith("{") and len(r["queries"]) == 2 and len(r["sources"]) == 2


def test_prompt_names_the_show(conn):
    show = single_act_show(conn)
    p = gemini.build_prompt(gemini._show(conn, show))
    assert "Zebulon in Los Angeles on September 17, 2026 featured Iguana Death Cult" in p


def test_opener_verified_on_page(conn):
    show = single_act_show(conn)
    client = FakeClient([GROUNDED], {"https://example.com/zebulon/iguana-death-cult": EVENT_PAGE})
    stats = gemini.ask(conn, client, **QUIET)
    assert stats["found"] == 1 and stats["verified"] == 1 and stats["search_queries"] == 2
    b = billing(conn, show)
    assert b[FLAT_WORMS][0] == "opener" and b[FLAT_WORMS][1] >= 0.4      # exported at the default threshold
    assert b[IDC][0] == "headliner"
    # Named by the model but on none of the fetched pages: kept, but too weak to export.
    made_up = b["name:made up band"]
    assert made_up[0] == "opener" and made_up[1] < 0.4
    c = conn.execute("SELECT url, extra FROM claims WHERE source = 'gemini' AND artist_id = ?",
                     (FLAT_WORMS,)).fetchone()
    assert c["url"] == "https://example.com/zebulon/iguana-death-cult"
    assert json.loads(c["extra"])["check"] == "page" and json.loads(c["extra"])["quote_on_page"]

    # Done: not asked again.
    stats = gemini.ask(conn, FakeClient([]), **QUIET)
    assert stats["requests"] == 0


def test_unreachable_pages_leave_it_unchecked(conn):
    show = single_act_show(conn)
    gemini.ask(conn, FakeClient([CAPTURED]), **QUIET)        # its redirect URL can't be fetched
    c = conn.execute("SELECT confidence, extra FROM claims WHERE source = 'gemini' AND artist_id = ?",
                     (FLAT_WORMS,)).fetchone()
    assert c["confidence"] == gemini.CONF_UNCHECKED and json.loads(c["extra"])["check"] == "unchecked"
    assert billing(conn, show)[FLAT_WORMS][0] == "opener"


def test_not_found_is_asked_again_later(conn):
    single_act_show(conn)
    empty = ok(json.dumps({"candidates": [{"content": {"parts": [{"text":
        '{"headliner": "Iguana Death Cult", "openers": [], "confident": false, "evidence": []}'}]}}]}))
    stats = gemini.ask(conn, FakeClient([empty]), **QUIET)
    assert stats["not_found"] == 1
    row = state.due(conn, "gemini") or [state.get(conn, "gemini", "show:1")]
    assert row[0]["interval_days"] == gemini.NOT_FOUND_MIN_DAYS
    assert conn.execute("SELECT COUNT(*) FROM claims WHERE source = 'gemini'").fetchone()[0] == 0


def test_only_single_act_shows_are_queued(conn):
    single_act_show(conn)
    v = ledger.get_or_create_venue(conn, "Zebulon", "Los Angeles")
    full = ledger.find_or_create_show(conn, "2026-08-01", v)
    ledger.add_claim(conn, full, IDC, "manual", "m1", role="headliner", confidence=1.0)
    ledger.add_claim(conn, full, FLAT_WORMS, "manual", "m1", role="opener", confidence=1.0)
    ledger.find_or_create_show(conn, "2099-01-01", v)                    # upcoming: not asked
    nobody = ledger.get_or_create_venue(conn, "Elsewhere", "Nowhere")    # no graph artist: not asked
    ledger.add_claim(conn, ledger.find_or_create_show(conn, "2026-01-01", nobody),
                     ledger.get_or_create_artist(conn, "Unknown Band"), "setlistfm", "s9")
    # Two acts, roles unknown (setlist.fm couldn't tell): skipped by default too.
    pair = ledger.find_or_create_show(conn, "2026-07-01", v)
    ledger.add_claim(conn, pair, IDC, "setlistfm", "s2")
    ledger.add_claim(conn, pair, ledger.get_or_create_artist(conn, "Not In Graph"), "setlistfm", "s3")
    assert gemini.queue_shows(conn) == 1
    assert gemini.queue_shows(conn, max_acts=2) == 3


def test_max_acts_widens_what_is_asked(conn):
    show = single_act_show(conn)
    ledger.add_claim(conn, show, ledger.get_or_create_artist(conn, "Other Band"), "setlistfm", "s2")
    client = FakeClient([CAPTURED])
    assert gemini.ask(conn, client, **QUIET)["requests"] == 0
    assert gemini.ask(conn, client, max_acts=2, **QUIET)["found"] == 1


def test_skips_a_show_another_source_filled_in(conn):
    show = single_act_show(conn)
    gemini.queue_shows(conn)
    ledger.add_claim(conn, show, FLAT_WORMS, "manual", "m1", role="opener", confidence=1.0)
    client = FakeClient([])
    stats = gemini.ask(conn, client, **QUIET)
    assert stats["skipped"] == 1 and client.prompts == []
    assert state.get(conn, "gemini", f"show:{show}") is None    # re-queued if it qualifies again


def test_429_ends_the_run_and_keeps_the_show_due(conn):
    single_act_show(conn)
    stats = gemini.ask(conn, FakeClient([status(429)]), **QUIET)
    assert stats["stopped"].startswith("HTTP 429")
    assert len(state.due(conn, "gemini")) == 1


def test_errors_back_off(conn):
    single_act_show(conn)
    stats = gemini.ask(conn, FakeClient([requests.ConnectionError()]), **QUIET)
    assert stats["errors"] == 1 and state.due(conn, "gemini") == []
    assert state.get(conn, "gemini", "show:1")["failures"] == 1


def test_daily_limit(conn, monkeypatch):
    monkeypatch.setattr(gemini, "DAILY_LIMIT", 1)
    single_act_show(conn, "2026-09-17")
    single_act_show(conn, "2026-09-20")
    client = FakeClient([CAPTURED, CAPTURED])
    gemini.ask(conn, client, **QUIET)
    assert len(client.prompts) == 1
    assert state.requests_today(conn, "gemini") == 1
    stats = gemini.ask(conn, client, **QUIET)
    assert stats["stopped"] == "daily limit" and len(client.prompts) == 1


def test_gemini_quota_day_is_pacific():
    # 03:00 UTC on Oct 18 is still Oct 17 in Los Angeles, where the free-tier quota resets.
    t = datetime(2026, 10, 18, 3, 0, tzinfo=timezone.utc)
    assert state.usage_day("gemini", t).isoformat() == "2026-10-17"
    assert state.usage_day("setlistfm", t).isoformat() == "2026-10-18"
