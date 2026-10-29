import json

from concertdb import db, msm_requested
from concertdb.sources import pollstar, setlistfm
from conftest import fixture_text

QUIET = {"log": lambda *_: None}
ZEBULON = {"id": "501234", "name": "Zebulon", "city": "Los Angeles", "state": "CA", "location": "Los Angeles, CA"}


def fetcher(rows):
    """fetch(after_seq) over a fixed list of (seq, source, data), recording what was asked."""
    calls = []

    def fetch(after):
        calls.append(after)
        return [r for r in rows if r[0] > after]
    fetch.calls = calls
    return fetch


def night():
    """The fixture night: several acts' setlists at one venue (setlist.fm's venue page)."""
    return json.loads(fixture_text("setlistfm_venue_night.json"))["setlist"]


def zebulon_events():
    return {"venue": ZEBULON, **json.loads(fixture_text("pollstar_events_zebulon.json"))}


def berlin_page():
    """A page of Pollstar's events in a city: two venues, one event without a venue."""
    return json.loads(fixture_text("pollstar_city_page.json"))


def claims(conn, source):
    q = ("SELECT native_id, role, confidence, position, extra FROM claims WHERE source = ?"
         " ORDER BY native_id, position, artist_id")
    return [dict(r) for r in conn.execute(q, (source,))]


def test_setlists_are_stored_like_the_setlistfm_crawl(conn, tmp_path):
    sls = night()
    fetch = fetcher([(i, "setlistfm", sl) for i, sl in enumerate(sls, start=1)])
    stats = msm_requested.import_requested(conn, fetch, **QUIET)
    assert stats["rows"] == len(sls) and stats["setlists"] == len(sls) and stats["unreadable"] == 0

    crawl = db.connect(tmp_path / "crawl.sqlite")
    for sl in sls:
        setlistfm.ingest_setlist(crawl, setlistfm.parse_setlist(sl))
    crawl.commit()
    assert claims(conn, "setlistfm") == claims(crawl, "setlistfm")

    # The next run asks only for what's newer; an unchanged setlist is skipped, an updated one stored.
    assert msm_requested.import_requested(conn, fetch, **QUIET)["rows"] == 0
    assert fetch.calls == [0, len(sls)]
    sl = sls[0]
    again = msm_requested.import_requested(conn, fetcher([(99, "setlistfm", sl)]), **QUIET)
    assert again["setlists_unchanged"] == 1
    updated = dict(sl, lastUpdated="2099-01-01T00:00:00.000+0000")
    assert msm_requested.import_requested(conn, fetcher([(100, "setlistfm", updated)]), **QUIET)["setlists"] == 1


def test_pollstar_events_are_stored_like_the_pollstar_crawl_and_marked(conn, tmp_path):
    stats = msm_requested.import_requested(conn, fetcher([(1, "pollstar", zebulon_events())]), **QUIET)
    n = len(pollstar.parse_events(zebulon_events()))
    assert (stats["pollstar_venues"], stats["pollstar_new_venues"], stats["pollstar_events"]) == (1, 1, n)

    crawl = db.connect(tmp_path / "crawl.sqlite")
    venue_id = pollstar._link(crawl, {"id": "501234", "name": "Zebulon", "city": "Los Angeles", "state": "CA"})
    for e in pollstar.parse_events(zebulon_events()):
        pollstar.ingest_event(crawl, e, venue_id)
    crawl.commit()
    ours, theirs = claims(conn, "pollstar"), claims(crawl, "pollstar")
    assert [json.loads(r.pop("extra")) for r in ours] == [dict(json.loads(r.pop("extra")), via="musicscenemap")
                                                         for r in theirs]
    assert ours == theirs

    # The venue is linked and queued for our own crawl, like `pollstar-add`; a second row doesn't link it again.
    assert conn.execute("SELECT 1 FROM crawl_state WHERE source = 'pollstar' AND key = 'venue:501234'").fetchone()
    again = msm_requested.import_requested(conn, fetcher([(2, "pollstar", zebulon_events())]), **QUIET)
    assert again["pollstar_new_venues"] == 0


def test_pollstar_city_pages_are_stored_like_the_pollstar_crawl_and_marked(conn, tmp_path):
    page = berlin_page()
    stats = msm_requested.import_requested(conn, fetcher([(1, "pollstar_city", page)]), **QUIET)
    assert (stats["pollstar_city_pages"], stats["pollstar_new_venues"], stats["pollstar_events"],
            stats["pollstar_events_without_venue"], stats["unreadable"]) == (1, 2, 3, 1, 0)

    # The same claims as crawling each venue's events page, marked as from MusicSceneMap.
    crawl = db.connect(tmp_path / "crawl.sqlite")
    for vid, name in (("272931", "Uber Arena"), ("280001", "Badehaus")):
        venue_id = pollstar._link(crawl, {"id": vid, "name": name, "city": "Berlin", "state": None})
        evs = [e for e in page["events"] if (e.get("venue") or {}).get("id") == int(vid)]
        for e in pollstar.parse_events({"events": evs}):
            pollstar.ingest_event(crawl, e, venue_id)
    crawl.commit()
    ours, theirs = claims(conn, "pollstar"), claims(crawl, "pollstar")
    assert [json.loads(r.pop("extra")) for r in ours] == [dict(json.loads(r.pop("extra")), via="musicscenemap")
                                                         for r in theirs]
    assert ours == theirs

    # Venues get the page's city, country and coordinates (the physical address), and aren't queued for a crawl.
    q = ("SELECT v.name, v.city, v.state, v.country, v.lat, v.lon FROM venues v"
         " JOIN venue_source_ids s ON s.venue_id = v.id WHERE s.source = 'pollstar' ORDER BY s.native_id")
    assert [tuple(r) for r in conn.execute(q)] == [("Uber Arena", "Berlin", None, "Germany", 52.5053, 13.4432),
                                                   ("Badehaus", "Berlin", None, "Germany", 52.5079, 13.4537)]
    assert not conn.execute("SELECT 1 FROM crawl_state WHERE source = 'pollstar'").fetchone()

    # The page again (a refetch): nothing new.
    n = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    again = msm_requested.import_requested(conn, fetcher([(2, "pollstar_city", page)]), **QUIET)
    assert again["pollstar_new_venues"] == 0
    assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == n


def test_city_page_venues_reuse_venues_we_already_have(conn):
    msm_requested.import_requested(conn, fetcher([(1, "pollstar", zebulon_events())]), **QUIET)
    event = dict(berlin_page()["events"][2], venue={"id": 501234, "name": "Zebulon", "location": "Los Angeles, CA"})
    stats = msm_requested.import_requested(conn, fetcher([(2, "pollstar_city", {"events": [event]})]), **QUIET)
    assert (stats["pollstar_new_venues"], stats["pollstar_events"]) == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0] == 1


def test_unknown_sources_and_unreadable_rows_are_skipped_not_fatal(conn):
    sl = night()[0]
    rows = [(1, "songkick", {}), (2, "setlistfm", {"id": "x"}), (3, "pollstar", {"venue": {"id": "9"}}),
            (4, "setlistfm", sl), (5, "pollstar_search", {"response": {"docs": []}}),
            (6, "pollstar_city", {"page": 0}), (7, "pollstar_city", {"events": [{"venue": {"id": 1, "name": "X"}, "playDate": "2026-11-01"}]})]
    stats = msm_requested.import_requested(conn, fetcher(rows), **QUIET)
    assert (stats["unknown_source"], stats["unreadable"], stats["setlists"], stats["skipped"]) == (1, 4, 1, 1)
    assert conn.execute("SELECT value FROM meta WHERE key = ?", (msm_requested.CURSOR_KEY,)).fetchone()["value"] == "7"
    assert conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0] == 1   # the setlist's; row 7 (an event without an id) left nothing
