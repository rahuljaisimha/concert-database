import gzip
import json

from concertdb import ledger, repair
from concertdb.sources import pollstar


def _old_merge(conn, venue, show_date, records):
    """Store records in ONE show, the way the old ±1-day rule did."""
    cur = conn.execute("INSERT INTO shows (date, venue_id, status, created_at, updated_at)"
                       " VALUES (?, ?, 'scheduled', 'x', 'x')", (show_date, venue))
    show = cur.lastrowid
    for source, native, artists, extra in records:
        for i, (name, role) in enumerate(artists):
            aid = ledger.get_or_create_artist(conn, name)
            ledger.add_claim(conn, show, aid, source, native, role=role, confidence=0.9 if role != "unknown" else 0.0,
                             position=i, extra=extra, resolve=False)
    ledger.resolve_show(conn, show)
    return show


def _bills(conn):
    out = {}
    for r in conn.execute("SELECT s.date, a.name, b.role FROM billings b JOIN shows s ON s.id = b.show_id"
                          " JOIN artists a ON a.id = b.artist_id ORDER BY s.date, a.name"):
        out.setdefault(r["date"], []).append((r["name"], r["role"]))
    return out


def test_split_merged_pollstar_events(conn, tmp_path):
    v = ledger.get_or_create_venue(conn, "The National", "Richmond")
    # Ashley McBryde's date is only in the raw archive (stored before claims kept their date).
    raw = tmp_path / "raw" / "pollstar"
    raw.mkdir(parents=True)
    body = {"events": [{"id": 9241618, "playDate": "2026-10-22T00:00:00", "name": "Ashley McBryde",
                        "artists": [{"name": "Ashley McBryde", "isHeadline": False}]}]}
    with gzip.open(raw / "2026-10-17.jsonl.gz", "wt") as f:
        f.write(json.dumps({"url": "https://www.pollstar.com/api/companies/1/events", "body": json.dumps(body)}) + "\n")
    # As stored live: McBryde's event (Oct 22) made the show; the Oct 23 co-headline show joined it.
    show = _old_merge(conn, v, "2026-10-22", [
        ("pollstar", "9241618", [("Ashley McBryde", "unknown")], {"title": "Ashley McBryde at The National"}),
        ("pollstar", "9262104", [("Snail Mail", "headliner"), ("Soccer Mommy", "headliner")], {"date": "2026-10-23"}),
    ])
    conn.commit()

    dry = repair.repair_shows(conn, tmp_path, dry_run=True, log=lambda *_: None)
    assert dry["shows_split"] == 1 and conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0] == 1

    stats = repair.repair_shows(conn, tmp_path, log=lambda *_: None)
    assert stats["shows_split"] == 1 and stats["records_moved"] == 1
    assert _bills(conn) == {
        "2026-10-22": [("Ashley McBryde", "unknown")],
        "2026-10-23": [("Snail Mail", "headliner"), ("Soccer Mommy", "headliner")],   # co-headliners: fine
    }
    assert conn.execute("SELECT id FROM shows WHERE date = '2026-10-22'").fetchone()[0] == show  # kept its id
    # Nothing left to do the second time.
    assert repair.repair_shows(conn, tmp_path, log=lambda *_: None)["shows_split"] == 0


def test_split_residency_and_reinfer_setlistfm_roles(conn, tmp_path):
    from concertdb.sources import setlistfm
    v = ledger.get_or_create_venue(conn, "The Chapel", "San Francisco")
    raw = tmp_path / "raw" / "setlistfm"
    raw.mkdir(parents=True)
    body = {"setlist": [{"id": "n2", "eventDate": "20-10-2026"}]}
    with gzip.open(raw / "2026-10-17.jsonl.gz", "wt") as f:
        f.write(json.dumps({"url": "https://api.setlist.fm/x", "body": json.dumps(body)}) + "\n")
    _old_merge(conn, v, "2026-10-19", [
        ("setlistfm", "n1", [("The Devil Makes Three", "headliner")], {"song_count": 18, "info": None, "tour": None}),
        ("setlistfm", "o1", [("Hovvdy", "opener")], {"song_count": 7, "info": "Opening for The Devil Makes Three",
                                                   "tour": None}),
        ("setlistfm", "n2", [("The Devil Makes Three", "unknown")], {"song_count": 17}),
    ])
    repair.repair_shows(conn, tmp_path, log=lambda *_: None)
    bills = _bills(conn)
    assert bills["2026-10-19"] == [("Hovvdy", "opener"), ("The Devil Makes Three", "headliner")]
    assert bills["2026-10-20"] == [("The Devil Makes Three", "unknown")]   # alone that night: no inferred role


def test_untouched_when_nothing_to_split(conn, tmp_path):
    v = ledger.get_or_create_venue(conn, "Zebulon", "Los Angeles")
    _old_merge(conn, v, "2026-09-17", [
        ("manual", "manual:2026-09-17:1", [("Iguana Death Cult", "headliner"), ("Flat Worms", "opener")], None),
        ("setlistfm", "x", [("Flat Worms", "unknown")], {"date": "2026-09-18"}),   # past midnight, shared act
    ])
    stats = repair.repair_shows(conn, tmp_path, log=lambda *_: None)
    assert stats["shows_checked"] == 1 and stats["shows_split"] == 0


def test_pollstar_events_on_consecutive_nights_are_two_shows(conn):
    v = ledger.get_or_create_venue(conn, "Starland Ballroom", "Sayreville")
    for eid, day, acts in [("1", "2026-10-20", ["The Story So Far", "Ultra Q"]), ("2", "2026-10-21", ["Insane Clown Posse"])]:
        pollstar.ingest_event(conn, {"id": eid, "date": day, "name": acts[0], "url": None, "is_festival": False,
                                     "artists": [{"name": a, "headline": i == 0} for i, a in enumerate(acts)]}, v)
    assert conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0] == 2
    assert _bills(conn)["2026-10-21"] == [("Insane Clown Posse", "unknown")]


def test_removes_empty_shows(conn, tmp_path):
    v = ledger.get_or_create_venue(conn, "Zebulon", "Los Angeles")
    ledger.find_or_create_show(conn, "2026-09-17", v)
    stats = repair.repair_shows(conn, tmp_path, log=lambda *_: None)
    assert stats["empty_shows_removed"] == 1 and stats["shows_after"] == 0
