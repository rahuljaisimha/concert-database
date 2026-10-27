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


def test_unknown_sources_and_unreadable_rows_are_skipped_not_fatal(conn):
    sl = night()[0]
    rows = [(1, "songkick", {}), (2, "setlistfm", {"id": "x"}), (3, "pollstar", {"venue": {"id": "9"}}),
            (4, "setlistfm", sl), (5, "pollstar_search", {"response": {"docs": []}})]
    stats = msm_requested.import_requested(conn, fetcher(rows), **QUIET)
    assert (stats["unknown_source"], stats["unreadable"], stats["setlists"], stats["skipped"]) == (1, 2, 1, 1)
    assert conn.execute("SELECT value FROM meta WHERE key = ?", (msm_requested.CURSOR_KEY,)).fetchone()["value"] == "5"
