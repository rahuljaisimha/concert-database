import json

import requests

from concertdb import state
from concertdb.sources import setlistfm
from conftest import FLAT_WORMS, OSEES, FakeSetlistClient, add_graph_artist, fixture_text, ok, setlist_page, status


def _sl(i, date="01-01-2024", mbid=OSEES, updated="2024-01-02T00:00:00.000+0000"):
    return {"id": f"sl{i}", "eventDate": date, "lastUpdated": updated,
            "artist": {"mbid": mbid, "name": "Osees"},
            "venue": {"id": f"v{i}", "name": f"Venue {i}", "city": {"name": "LA"}},
            "sets": {"set": []}}


def test_parse_setlist():
    data = json.loads(fixture_text("setlistfm_artist_page1.json"))
    p = setlistfm.parse_setlist(data["setlist"][0])
    assert p["date"] == "2025-04-12"
    assert p["artist_mbid"] == OSEES
    assert p["venue_name"] == "The Echo" and p["city"] == "Los Angeles"
    assert p["song_count"] == 12
    assert p["tour"] == "Spring Tour 2025"
    assert p["lat"] == 34.052


def test_same_night_infers_opener(conn):
    add_graph_artist(conn, OSEES, "Osees")
    add_graph_artist(conn, FLAT_WORMS, "Flat Worms")
    for sl in json.loads(fixture_text("setlistfm_venue_night.json"))["setlist"]:
        setlistfm.ingest_setlist(conn, setlistfm.parse_setlist(sl))
    rows = {r["artist_id"]: r for r in conn.execute("SELECT * FROM billings")}
    assert rows[OSEES]["role"] == "headliner"
    assert rows[FLAT_WORMS]["role"] == "opener"
    assert conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0] == 1
    # The venue was registered for a venue crawl.
    assert state.get(conn, "setlistfm", "venue:4bd6cf2e") is not None


def test_backfill_then_incremental(conn):
    state.ensure(conn, "setlistfm", f"artist:{OSEES}", priority=5)
    page1 = setlist_page([_sl(1), _sl(2)], total=3, per_page=2)
    page2 = setlist_page([_sl(3)], total=3, page=2, per_page=2)
    client = FakeSetlistClient({("artist", OSEES): [page1, page2]})

    stats = setlistfm.crawl(conn, client, max_requests=10, log=lambda *_: None)
    assert stats["new"] == 3
    assert [c[2] for c in client.calls if c[0] == "artist"] == [1, 2]
    row = state.get(conn, "setlistfm", f"artist:{OSEES}")
    assert state.get_cursor(row) == {"backfill_done": True}
    assert row["next_due"] > row["last_checked"]

    # Next visit: page 1 has nothing new → one request, stop.
    conn.execute("UPDATE crawl_state SET next_due = '2000-01-01' WHERE key = ?", (f"artist:{OSEES}",))
    conn.execute("DELETE FROM crawl_state WHERE key LIKE 'venue:%'")
    client.calls.clear()
    stats = setlistfm.crawl(conn, client, max_requests=10, log=lambda *_: None)
    assert client.calls == [("artist", OSEES, 1)]
    assert stats["new"] == 0
    assert state.get(conn, "setlistfm", f"artist:{OSEES}")["interval_days"] == 28  # doubled from 14


def test_budget_exhausted_resumes_from_cursor(conn):
    state.ensure(conn, "setlistfm", f"artist:{OSEES}")
    pages = [setlist_page([_sl(i)], total=3, page=i, per_page=1) for i in (1, 2, 3)]
    client = FakeSetlistClient({("artist", OSEES): pages})
    setlistfm.crawl(conn, client, max_requests=1, log=lambda *_: None)
    row = state.get(conn, "setlistfm", f"artist:{OSEES}")
    assert state.get_cursor(row) == {"next_page": 2}
    assert row["last_checked"] is None  # still due

    conn.execute("DELETE FROM crawl_state WHERE key LIKE 'venue:%'")
    client.calls.clear()
    setlistfm.crawl(conn, client, max_requests=10, log=lambda *_: None)
    assert [c[2] for c in client.calls if c[0] == "artist"] == [2, 3]
    assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 3


def test_not_found_is_postponed(conn):
    state.ensure(conn, "setlistfm", "artist:unknown-mbid")
    client = FakeSetlistClient({})
    setlistfm.crawl(conn, client, max_requests=5, log=lambda *_: None)
    row = state.get(conn, "setlistfm", "artist:unknown-mbid")
    assert row["interval_days"] == setlistfm.NOT_FOUND_DAYS
    assert len(client.calls) == 1


class SequenceClient:
    """Returns the given responses in order, whatever page is asked for."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.interval = 1.0

    def page(self, kind, ident, page):
        self.calls.append((kind, ident, page))
        return self.responses.pop(0) if self.responses else status(404)

    def slow_down(self):
        self.interval *= 1.5
        return self.interval


def test_rate_limit_waits_and_retries_same_page(conn, monkeypatch):
    waits = []
    monkeypatch.setattr(setlistfm, "sleep", waits.append)
    state.ensure(conn, "setlistfm", f"artist:{OSEES}")
    limited = status(429)
    limited.retry_after = 45
    client = SequenceClient([limited, status(429), setlist_page([_sl(1)])])
    stats = setlistfm.crawl(conn, client, max_requests=10, log=lambda *_: None)
    assert [c[2] for c in client.calls if c[0] == "artist"] == [1, 1, 1]
    assert waits[:2] == [45, setlistfm.BACKOFF_SECONDS[1]]   # Retry-After honored when longer
    assert client.interval == 2.25                             # slowed down twice
    assert stats["rate_limited"] == 2 and stats["new"] == 1


def test_repeated_rate_limit_stops_run(conn, monkeypatch):
    monkeypatch.setattr(setlistfm, "sleep", lambda s: None)
    state.ensure(conn, "setlistfm", "artist:a")
    state.ensure(conn, "setlistfm", "artist:b")
    client = SequenceClient([status(429)] * 20)
    stats = setlistfm.crawl(conn, client, max_requests=50, log=lambda *_: None)
    assert stats["requests"] == len(setlistfm.BACKOFF_SECONDS) + 1
    assert "_streak" not in stats


class Flaky(SequenceClient):
    """SequenceClient where an exception in the list is raised instead of returned."""

    def page(self, kind, ident, page):
        r = super().page(kind, ident, page)
        if isinstance(r, Exception):
            raise r
        return r


def test_network_error_waits_and_retries_same_page(conn, monkeypatch):
    waits = []
    monkeypatch.setattr(setlistfm, "sleep", waits.append)
    state.ensure(conn, "setlistfm", f"artist:{OSEES}")
    client = Flaky([requests.exceptions.ReadTimeout("read timed out"), requests.exceptions.ConnectionError("reset"),
                    setlist_page([_sl(1)])])
    stats = setlistfm.crawl(conn, client, max_requests=10, log=lambda *_: None)
    assert [c[2] for c in client.calls if c[0] == "artist"] == [1, 1, 1]
    assert waits == list(setlistfm.NETWORK_BACKOFF_SECONDS[:2])
    assert stats["network_errors"] == 2 and stats["new"] == 1 and "stopped" not in stats
    assert state.requests_today(conn, "setlistfm") == stats["requests"]   # failed requests count too


def test_setlistfm_down_ends_the_run_cleanly(conn, monkeypatch):
    monkeypatch.setattr(setlistfm, "sleep", lambda s: None)
    state.ensure(conn, "setlistfm", f"artist:{OSEES}")
    client = Flaky([setlist_page([_sl(1)], total=60)] + [requests.exceptions.ReadTimeout("x")] * 10)
    stats = setlistfm.crawl(conn, client, max_requests=50, log=lambda *_: None)
    assert stats["network_errors"] == len(setlistfm.NETWORK_BACKOFF_SECONDS) + 1
    assert stats["stopped"].startswith("setlist.fm not answering (ReadTimeout")
    assert stats["new"] == 1 and "_net_streak" not in stats
    cur = state.get_cursor(state.get(conn, "setlistfm", f"artist:{OSEES}"))
    assert cur["next_page"] == 2                                      # resumes at the page that failed


def test_daily_limit_respected(conn, monkeypatch):
    monkeypatch.setattr(setlistfm, "DAILY_LIMIT", 2)
    for k in "abc":
        state.ensure(conn, "setlistfm", f"artist:{k}")
    pages = {("artist", k): [setlist_page([_sl(k)])] for k in "abc"}
    client = FakeSetlistClient(pages)
    setlistfm.crawl(conn, client, max_requests=100, log=lambda *_: None)
    assert len(client.calls) == 2
    assert state.requests_today(conn, "setlistfm") == 2


def test_updated_setlist_is_reingested(conn):
    state.ensure(conn, "setlistfm", f"artist:{OSEES}")
    client = FakeSetlistClient({("artist", OSEES): [setlist_page([_sl(1)])]})
    setlistfm.crawl(conn, client, max_requests=5, log=lambda *_: None)
    conn.execute("UPDATE crawl_state SET next_due = '2000-01-01'")
    client.pages[("artist", OSEES)] = [setlist_page([_sl(1, updated="2024-05-05T00:00:00.000+0000")])]
    stats = setlistfm.crawl(conn, client, max_requests=5, log=lambda *_: None)
    assert stats["new"] == 1


def _pages(n):
    return [setlist_page([_sl(i)], total=n, page=i, per_page=1) for i in range(1, n + 1)]


def test_first_pass_is_capped_then_deep_pass_finishes(conn, monkeypatch):
    monkeypatch.setattr(setlistfm, "FIRST_PASS_PAGES", 2)
    state.ensure(conn, "setlistfm", f"artist:{OSEES}", priority=50)
    state.ensure(conn, "setlistfm", "artist:other", priority=10)
    client = FakeSetlistClient({("artist", OSEES): _pages(5), ("artist", "other"): _pages(1)})
    setlistfm.crawl(conn, client, max_requests=100, log=lambda *_: None)
    artist_calls = [(c[1], c[2]) for c in client.calls if c[0] == "artist"]
    # Osees: newest 2 pages, then "other" gets its turn, then Osees' older pages.
    assert artist_calls == [(OSEES, 1), (OSEES, 2), ("other", 1), (OSEES, 3), (OSEES, 4), (OSEES, 5)]
    row = state.get(conn, "setlistfm", f"artist:{OSEES}")
    assert state.get_cursor(row) == {"backfill_done": True}
    assert conn.execute("SELECT COUNT(*) FROM claims WHERE artist_id = ?", (OSEES,)).fetchone()[0] == 5


def test_deep_pass_waits_for_first_passes(conn, monkeypatch):
    monkeypatch.setattr(setlistfm, "FIRST_PASS_PAGES", 1)
    state.ensure(conn, "setlistfm", f"artist:{OSEES}", priority=50)
    client = FakeSetlistClient({("artist", OSEES): _pages(3)})
    setlistfm.crawl(conn, client, max_requests=1, log=lambda *_: None)
    setlistfm.crawl(conn, client, max_requests=1, log=lambda *_: None)   # only defers, no request
    row = state.get(conn, "setlistfm", f"artist:{OSEES}")
    assert state.get_cursor(row) == {"deep": True, "next_page": 2}
    assert row["priority"] == setlistfm.DEEP_PRIORITY and row["last_checked"] is not None
    assert len([c for c in client.calls if c[0] == "artist"]) == 1


def test_in_progress_cursor_beyond_cap_is_deferred(conn):
    """An artist that was mid-history before the cap existed (e.g. page 48)."""
    state.ensure(conn, "setlistfm", f"artist:{OSEES}", priority=50)
    state.save_cursor(conn, "setlistfm", f"artist:{OSEES}", {"next_page": 48})
    client = FakeSetlistClient({})
    stats = setlistfm.crawl(conn, client, max_requests=1, log=lambda *_: None)
    assert stats["deferred"] == 1
    assert state.get_cursor(state.get(conn, "setlistfm", f"artist:{OSEES}")) == {"deep": True, "next_page": 48}


def test_prioritized_artists_are_not_capped(conn, monkeypatch):
    monkeypatch.setattr(setlistfm, "FIRST_PASS_PAGES", 1)
    state.ensure(conn, "setlistfm", f"artist:{OSEES}", priority=setlistfm.NO_CAP_PRIORITY)
    client = FakeSetlistClient({("artist", OSEES): _pages(3)})
    setlistfm.crawl(conn, client, max_requests=10, log=lambda *_: None)
    assert [c[2] for c in client.calls if c[0] == "artist"] == [1, 2, 3]


def test_interrupted_recheck_resumes_at_its_page(conn):
    """A recheck stopped after page 1 continues at page 2, not page 1 (now all known)."""
    key = f"artist:{OSEES}"
    state.ensure(conn, "setlistfm", key)
    state.save_cursor(conn, "setlistfm", key, {"backfill_done": True})
    pages = [setlist_page([_sl(i)], total=3, page=i, per_page=1) for i in (1, 2, 3)]
    client = FakeSetlistClient({("artist", OSEES): pages})
    setlistfm.crawl(conn, client, max_requests=1, log=lambda *_: None)
    assert state.get_cursor(state.get(conn, "setlistfm", key)) == {"backfill_done": True, "recheck_page": 2}

    client.calls.clear()
    conn.execute("DELETE FROM crawl_state WHERE key LIKE 'venue:%'")
    setlistfm.crawl(conn, client, max_requests=10, log=lambda *_: None)
    assert [c[2] for c in client.calls if c[0] == "artist"] == [2, 3]
    row = state.get(conn, "setlistfm", key)
    assert state.get_cursor(row) == {"backfill_done": True}
    assert row["interval_days"] == setlistfm.RECHECK_MIN_DAYS   # counted as changed


def test_no_write_lock_held_during_requests_or_backoff(tmp_path, monkeypatch):
    import sqlite3

    from concertdb import db
    path = tmp_path / "ledger.sqlite"
    conn = db.connect(path)
    checks = []

    def check(*_):
        other = sqlite3.connect(path, timeout=0.1)
        other.execute("BEGIN IMMEDIATE")   # "database is locked" if this process held the lock
        other.rollback()
        other.close()
        checks.append(1)

    monkeypatch.setattr(setlistfm, "sleep", check)
    state.ensure(conn, "setlistfm", f"artist:{OSEES}")
    conn.commit()

    class Checking(SequenceClient):
        def page(self, kind, ident, page):
            check()
            return super().page(kind, ident, page)

    pages = [setlist_page([_sl(1), _sl(2)], total=4, per_page=2),
             status(429),
             setlist_page([_sl(3), _sl(4)], total=4, page=2, per_page=2)]
    stats = setlistfm.crawl(conn, Checking(pages), max_requests=10, log=lambda *_: None)
    assert stats["new"] == 4
    assert len(checks) >= 4   # page 1, page 2 (429), backoff sleep, page 2, then the venues found
