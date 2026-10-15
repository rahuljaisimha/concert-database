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
