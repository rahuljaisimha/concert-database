import json

from concertdb import ledger, state
from concertdb.sources import pollstar
from conftest import FLAT_WORMS, add_graph_artist, fixture_text, ok, status


class FakeClient:
    def __init__(self, ac=None, events=None):
        self.ac = ac or {}
        self.ev = events or {}
        self.calls = []

    def autocomplete(self, q):
        self.calls.append(("ac", q))
        return self.ac.get(q, ok(json.dumps({"response": {"docs": []}})))

    def events(self, pid, page):
        self.calls.append(("events", pid, page))
        return self.ev.get(pid, status(404))


AC = ok(fixture_text("pollstar_autocomplete_zebulon.json"))
EV = ok(fixture_text("pollstar_events_zebulon.json"))


def test_match_venue_needs_same_city():
    venues = pollstar.parse_venues(json.loads(fixture_text("pollstar_autocomplete_zebulon.json")))
    assert [v["id"] for v in venues] == ["501234", "777"]          # artists dropped, by rank
    assert pollstar.match_venue(venues, "Zebulon", "Los Angeles")["id"] == "501234"
    assert pollstar.match_venue(venues, "Zebulon", "Brooklyn")["id"] == "777"
    assert pollstar.match_venue(venues, "Zebulon", "Chicago") is None


def test_roles():
    assert pollstar.assign_roles([{"name": "A", "headline": True}, {"name": "B", "headline": False}]) == [
        ("A", "headliner", 0.9), ("B", "opener", 0.9)]
    assert pollstar.assign_roles([{"name": "A", "headline": False}, {"name": "B", "headline": False}]) == [
        ("A", "headliner", 0.6), ("B", "opener", 0.6)]
    assert pollstar.assign_roles([{"name": "A", "headline": False}]) == [("A", "unknown", 0.0)]


def test_add_venue_and_capture(conn):
    add_graph_artist(conn, FLAT_WORMS, "Flat Worms")
    client = FakeClient({"Zebulon": AC}, {"501234": EV})
    assert pollstar.add_venue(conn, client, "Zebulon", "Los Angeles")["id"] == "501234"
    stats = pollstar.capture(conn, client, log=lambda *_: None)
    assert stats["events"] == 3
    show = conn.execute("SELECT id FROM shows WHERE date = '2026-09-17'").fetchone()["id"]
    roles = {r["artist_id"]: r["role"] for r in conn.execute("SELECT * FROM billings WHERE show_id = ?", (show,))}
    assert roles[FLAT_WORMS] == "opener"
    assert roles["name:iguana death cult"] == "headliner"

    # Not due again for 3 days; when it is, an unchanged list stores nothing and backs off.
    client.calls.clear()
    pollstar.capture(conn, client, log=lambda *_: None)
    assert client.calls == []
    conn.execute("UPDATE crawl_state SET next_due = '2000-01-01' WHERE key = 'venue:501234'")
    stats = pollstar.capture(conn, client, log=lambda *_: None)
    assert stats["unchanged"] == 1
    assert state.get(conn, "pollstar", "venue:501234")["interval_days"] == 6


def test_ledger_venues_are_looked_up_once(conn):
    v = ledger.get_or_create_venue(conn, "Zebulon", "Los Angeles")
    ledger.find_or_create_show(conn, "2026-01-10", v)
    other = ledger.get_or_create_venue(conn, "Nowhere Club", "Los Angeles")
    ledger.find_or_create_show(conn, "2026-01-11", other)
    client = FakeClient({"Zebulon": AC}, {"501234": EV})
    pollstar.capture(conn, client, log=lambda *_: None)
    assert ("ac", "Zebulon") in client.calls and ("ac", "Nowhere Club") in client.calls
    assert conn.execute("SELECT venue_id FROM venue_source_ids WHERE source = 'pollstar'").fetchone()[0] == v

    client.calls.clear()
    conn.execute("UPDATE crawl_state SET next_due = '2000-01-01' WHERE key LIKE 'venue:%'")
    pollstar.capture(conn, client, log=lambda *_: None)
    assert [c for c in client.calls if c[0] == "ac"] == []   # neither the match nor the miss is re-asked


def test_budget(conn, monkeypatch):
    monkeypatch.setattr(pollstar, "DAILY_LIMIT", 1)
    for name in ("A Club", "B Club"):
        v = ledger.get_or_create_venue(conn, name, "LA")
        ledger.find_or_create_show(conn, "2026-01-10", v)
    client = FakeClient()
    pollstar.capture(conn, client, log=lambda *_: None)
    assert len(client.calls) == 1


class SequenceClient(FakeClient):
    """Events/autocomplete answers come from one queue, in order (Response or exception)."""

    def __init__(self, responses, ac=None):
        super().__init__(ac=ac)
        self.queue = list(responses)
        self.interval = 1.0

    def _next(self):
        r = self.queue.pop(0) if self.queue else status(404)
        if isinstance(r, Exception):
            raise r
        return r

    def events(self, pid, page):
        self.calls.append(("events", pid, page))
        return self._next()

    def slow_down(self):
        self.interval *= 1.5
        return self.interval


def _watch_zebulon(conn):
    pollstar._link(conn, {"id": "501234", "name": "Zebulon", "city": "Los Angeles", "state": "CA"})
    conn.commit()


def test_pushback_waits_slows_and_retries(conn, monkeypatch):
    waits = []
    monkeypatch.setattr(pollstar, "sleep", waits.append)
    _watch_zebulon(conn)
    limited = status(429)
    limited.retry_after = 90
    client = SequenceClient([limited, status(503), EV])
    stats = pollstar.capture(conn, client, log=lambda *_: None)
    assert [c[2] for c in client.calls] == [0, 0, 0]           # same page retried
    assert waits == [90, pollstar.BACKOFF_SECONDS[1]]          # Retry-After honored when longer
    assert client.interval == 2.25                             # slowed down twice
    assert stats["pushback"] == 2 and stats["events"] == 3
    assert state.requests_today(conn, "pollstar") == 3


def test_network_error_is_pushback(conn, monkeypatch):
    import requests
    monkeypatch.setattr(pollstar, "sleep", lambda s: None)
    _watch_zebulon(conn)
    client = SequenceClient([requests.ConnectionError("reset"), EV])
    stats = pollstar.capture(conn, client, log=lambda *_: None)
    assert stats["pushback"] == 1 and stats["events"] == 3


def test_repeated_pushback_pauses_all_processes(conn, monkeypatch):
    monkeypatch.setattr(pollstar, "sleep", lambda s: None)
    _watch_zebulon(conn)
    client = SequenceClient([status(403)] * 20)
    lines = []
    stats = pollstar.capture(conn, client, log=lines.append)
    assert stats["requests"] == len(pollstar.BACKOFF_SECONDS) + 1
    assert pollstar.paused_until(conn)
    assert "paused until" in lines[-1]
    # Another run (e.g. `concertdb run` after the Pollstar process stopped) sends nothing.
    conn.execute("UPDATE crawl_state SET next_due = '2000-01-01'")
    client.calls.clear()
    assert pollstar.capture(conn, client, log=lambda *_: None)["requests"] == 0
    assert client.calls == []


def test_long_run_rereads_due_work(conn, monkeypatch):
    monkeypatch.setattr(pollstar, "DUE_BATCH", 1)
    for name in ("A Club", "B Club", "C Club"):
        v = ledger.get_or_create_venue(conn, name, "LA")
        ledger.find_or_create_show(conn, "2026-01-10", v)
    client = FakeClient()
    pollstar.capture(conn, client, log=lambda *_: None)
    assert sorted(c[1] for c in client.calls) == ["A Club", "B Club", "C Club"]


def _assert_db_writable_by_another_process(path):
    """What a second crawler process would do while this one waits on the network."""
    import sqlite3
    other = sqlite3.connect(path, timeout=0.1)
    try:
        other.execute("BEGIN IMMEDIATE")   # raises "database is locked" if a write lock is held
        other.rollback()
    finally:
        other.close()


def test_no_write_lock_held_during_requests_or_backoff(tmp_path, monkeypatch):
    from concertdb import db
    path = tmp_path / "ledger.sqlite"
    conn = db.connect(path)
    checks = []

    def check(*_):
        _assert_db_writable_by_another_process(path)
        checks.append(1)

    monkeypatch.setattr(pollstar, "sleep", check)
    v = ledger.get_or_create_venue(conn, "Zebulon", "Los Angeles")
    ledger.find_or_create_show(conn, "2026-01-10", v)
    conn.commit()

    class Checking(SequenceClient):
        def autocomplete(self, q):
            check()
            return AC

        def events(self, pid, page):
            check()
            return super().events(pid, page)

    client = Checking([status(429), EV])
    stats = pollstar.capture(conn, client, log=lambda *_: None)
    assert stats["events"] == 3
    assert len(checks) == 4   # autocomplete, events (429), backoff sleep, events


def test_events_request_one_large_page():
    class Recorder:
        params = None

        def get(self, source, url, params=None, headers=None, **kw):
            self.params = params
            return EV

    rec = Recorder()
    pollstar.Client(rec).events("501234", 0)
    assert rec.params["pageSize"] == "1000" and rec.params["page"] == "0"


ESCAPED = ok(json.dumps({"response": {"docs": [
    {"EntityId": 300487, "ListName": "Stubb&#39;s Waller Creek Amphitheatre", "EntityType": "Venue",
     "City": "Austin", "State": "TX", "Rank": 30},
    {"EntityId": 42, "ListName": "Catalina Bar &amp; Grill", "EntityType": "Venue", "City": "Los Angeles",
     "Rank": 20}]}}))


def test_escaped_names_are_unescaped_and_match():
    venues = pollstar.parse_venues(ESCAPED.json())
    assert [v["name"] for v in venues] == ["Stubb's Waller Creek Amphitheatre", "Catalina Bar & Grill"]
    assert pollstar.match_venue(venues, "Stubb's Waller Creek Amphitheatre", "Austin")["id"] == "300487"
    assert pollstar.match_venue(venues, "Catalina Bar and Grill", "Los Angeles")["id"] == "42"
    events = pollstar.parse_events({"events": [{"id": 1, "playDate": "2026-11-01T00:00:00", "name": "A &amp; B",
                                                "artists": [{"name": "Judah &amp; The Lion"}]}]})
    assert events[0]["name"] == "A & B" and events[0]["artists"][0]["name"] == "Judah & The Lion"


def test_norm_unescapes():
    from concertdb.normalize import norm
    assert norm("Stubb&#39;s") == norm("Stubb's")
    assert norm("Bar &amp; Grill") == norm("Bar & Grill") == "bar and grill"


def test_rematch_earlier_misses_from_archive(conn, tmp_path):
    import gzip
    from urllib.parse import quote
    v = ledger.get_or_create_venue(conn, "Stubb's Waller Creek Amphitheatre", "Austin")
    ledger.find_or_create_show(conn, "2026-01-10", v)
    other = ledger.get_or_create_venue(conn, "Nowhere Club", "Austin")
    ledger.find_or_create_show(conn, "2026-01-10", other)
    # Both looked up before the fix: both stored as "no match".
    pollstar.queue_venues_to_resolve(conn)
    for vid in (v, other):
        state.postpone(conn, "pollstar", f"resolve:{vid}", pollstar.NO_MATCH_DAYS)
    raw = tmp_path / "raw" / "pollstar"
    raw.mkdir(parents=True)
    with gzip.open(raw / "2026-10-17.jsonl.gz", "wt") as f:
        for q, body in (("Stubb's Waller Creek Amphitheatre", ESCAPED.text), ("Nowhere Club", '{"response": {"docs": []}}')):
            f.write(json.dumps({"url": pollstar.AUTOCOMPLETE.format(q=quote(q, safe="")), "body": body}) + "\n")
    stats = pollstar.rematch_archived(conn, tmp_path, log=lambda *_: None)
    assert stats == {"misses": 2, "archived": 2, "matched": 1}
    assert conn.execute("SELECT venue_id FROM venue_source_ids WHERE source = 'pollstar' AND native_id = '300487'"
                        ).fetchone()[0] == v
    assert state.get(conn, "pollstar", "venue:300487") is not None        # its event list is now due
    assert pollstar.rematch_archived(conn, tmp_path, log=lambda *_: None)["matched"] == 0
