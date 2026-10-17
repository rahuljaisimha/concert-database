from concertdb import state
from concertdb.sources import jsonld
from conftest import FLAT_WORMS, add_graph_artist, fixture_text, ok


def test_split_title():
    assert jsonld.split_title("Iguana Death Cult w/ Flat Worms") == ["Iguana Death Cult", "Flat Worms"]
    assert jsonld.split_title("Doom Gong w/ Some Opener, Another Band") == ["Doom Gong", "Some Opener", "Another Band"]
    assert jsonld.split_title("Headliner with special guests") == ["Headliner"]
    assert jsonld.split_title("Florence and the Machine (Sold Out)") == ["Florence and the Machine"]
    assert jsonld.split_title("A + B") == ["A", "B"]


def test_event_page_iguana_death_cult_flat_worms(conn):
    """The show that started this project: 2026-09-17, Zebulon, Flat Worms opening."""
    add_graph_artist(conn, FLAT_WORMS, "Flat Worms")
    events = [jsonld.parse_event(e) for e in jsonld.extract_events(fixture_text("event_page.html"))]
    assert len(events) == 1
    e = events[0]
    assert e["date"] == "2026-09-17" and e["venue_name"] == "Zebulon" and e["city"] == "Los Angeles"
    assert e["acts"] == ["Iguana Death Cult", "Flat Worms"]

    show_id = jsonld.ingest_event(conn, e, "https://example.com/event/iguana-death-cult-zebulon")
    roles = {r["artist_id"]: r["role"] for r in conn.execute(
        "SELECT * FROM billings WHERE show_id = ?", (show_id,))}
    assert roles == {"name:iguana death cult": "headliner", FLAT_WORMS: "opener"}


def test_calendar_page(conn):
    events = [jsonld.parse_event(e) for e in jsonld.extract_events(fixture_text("calendar_page.html"))]
    by_date = {e["date"]: e for e in events}
    assert set(by_date) == {"2026-09-27", "2026-10-02", "2026-10-05"}  # the Organization is skipped
    assert by_date["2026-09-27"]["acts"] == ["Doom Gong", "Some Opener", "Another Band"]
    assert by_date["2026-10-02"]["acts"] == ["Florence and the Machine"]
    assert by_date["2026-10-05"]["status"] == "cancelled"
    assert by_date["2026-10-05"]["acts"] == ["Cancelled Band", "Opener Band"]
    for e in events:
        jsonld.ingest_event(conn, e, "https://example.com/calendar")
    assert conn.execute("SELECT status FROM shows WHERE date = '2026-10-05'").fetchone()[0] == "cancelled"
    # A single act gets no role.
    assert conn.execute(
        "SELECT role FROM billings b JOIN shows s ON s.id = b.show_id WHERE s.date = '2026-10-02'"
    ).fetchone()[0] == "unknown"


class FakeFetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, source, url, etag=None, last_modified=None, **_):
        self.calls.append((url, etag))
        return self.responses[url]


def test_capture_skips_unchanged_pages(conn):
    url = "https://example.com/calendar"
    jsonld.add_url(conn, url)
    f = FakeFetcher({url: ok(fixture_text("calendar_page.html"), etag='"v1"')})
    stats = jsonld.capture(conn, f, log=lambda *_: None)
    assert stats["events"] == 3
    row = state.get(conn, "jsonld", f"url:{url}")
    assert row["etag"] == '"v1"' and row["interval_days"] == 1

    conn.execute("UPDATE crawl_state SET next_due = '2000-01-01'")
    stats = jsonld.capture(conn, f, log=lambda *_: None)
    assert stats["unchanged"] == 1 and stats["events"] == 0
    assert f.calls[-1] == (url, '"v1"')  # conditional request
    assert state.get(conn, "jsonld", f"url:{url}")["interval_days"] == 2
