from concertdb import ledger
from conftest import FLAT_WORMS, add_graph_artist


def test_artist_resolution(conn):
    add_graph_artist(conn, FLAT_WORMS, "Flat Worms")
    assert ledger.get_or_create_artist(conn, "The Flat Worms") == FLAT_WORMS
    assert ledger.get_or_create_artist(conn, "Iguana Death Cult") == "name:iguana death cult"
    # Two graph artists with the same name: don't guess.
    add_graph_artist(conn, "m1", "Jack White", "person")
    add_graph_artist(conn, "m2", "Jack White", "person")
    assert ledger.get_or_create_artist(conn, "Jack White") == "name:jack white"
    assert ledger.get_or_create_artist(conn, "Jack White", "m2") == "m2"


def test_venue_matching(conn):
    v1 = ledger.get_or_create_venue(conn, "Zebulon", "Los Angeles", source="setlistfm", native_id="abc")
    assert ledger.get_or_create_venue(conn, "zebulon", "los angeles") == v1
    assert ledger.get_or_create_venue(conn, "Zebulon Cafe Renamed", "LA", source="setlistfm", native_id="abc") == v1
    assert ledger.get_or_create_venue(conn, "Zebulon", "Paris") != v1


def _claim(conn, show, artist, source, native, role="unknown"):
    ledger.add_claim(conn, show, artist, source, native, role=role, confidence=0.9)


def test_next_day_merges_only_with_a_shared_act(conn):
    v = ledger.get_or_create_venue(conn, "Zebulon", "Los Angeles")
    fw, idc = ledger.get_or_create_artist(conn, "Flat Worms"), ledger.get_or_create_artist(conn, "Iguana Death Cult")
    s = ledger.find_or_create_show(conn, "2026-09-17", v, [fw], source="pollstar", native_id="e1")
    _claim(conn, s, fw, "pollstar", "e1")
    # Same act, dated past midnight by another source: the same show.
    assert ledger.find_or_create_show(conn, "2026-09-18", v, [fw], source="setlistfm", native_id="sl1") == s
    # Another act the next night: a different show.
    assert ledger.find_or_create_show(conn, "2026-09-18", v, [idc], source="setlistfm", native_id="sl2") != s
    assert ledger.find_or_create_show(conn, "2026-09-17", v, [idc], source="setlistfm", native_id="sl3") == s
    assert ledger.find_or_create_show(conn, "2026-09-20", v) != s


def test_different_events_from_one_source_stay_apart(conn):
    v = ledger.get_or_create_venue(conn, "The National", "Richmond")
    a, b = ledger.get_or_create_artist(conn, "Snail Mail"), ledger.get_or_create_artist(conn, "Ashley McBryde")
    s = ledger.find_or_create_show(conn, "2026-10-23", v, [a], source="pollstar", native_id="9262104")
    _claim(conn, s, a, "pollstar", "9262104", "headliner")
    # Another Pollstar event the night before, or even the same night (early/late show).
    assert ledger.find_or_create_show(conn, "2026-10-22", v, [b], source="pollstar", native_id="9241618") != s
    assert ledger.find_or_create_show(conn, "2026-10-23", v, [b], source="pollstar", native_id="9241618") != s
    # The same event seen again keeps its show.
    assert ledger.find_or_create_show(conn, "2026-10-23", v, [a], source="pollstar", native_id="9262104") == s


def test_residency_nights_stay_apart(conn):
    """Same band, same venue, consecutive nights: different setlists, different shows."""
    v = ledger.get_or_create_venue(conn, "The Chapel", "San Francisco")
    dm3 = ledger.get_or_create_artist(conn, "The Devil Makes Three")
    s = ledger.find_or_create_show(conn, "2026-10-19", v, [dm3], source="setlistfm", native_id="a")
    _claim(conn, s, dm3, "setlistfm", "a")
    assert ledger.find_or_create_show(conn, "2026-10-20", v, [dm3], source="setlistfm", native_id="b") != s


def test_moved_event_leaves_no_empty_show(conn):
    v = ledger.get_or_create_venue(conn, "Zebulon", "Los Angeles")
    a = ledger.get_or_create_artist(conn, "Flat Worms")
    s1 = ledger.find_or_create_show(conn, "2026-09-17", v, [a], source="pollstar", native_id="e1")
    _claim(conn, s1, a, "pollstar", "e1")
    s2 = ledger.find_or_create_show(conn, "2026-11-02", v, [a], source="pollstar", native_id="e1")  # rescheduled
    assert s2 != s1
    _claim(conn, s2, a, "pollstar", "e1")
    assert conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0] == 1


def test_resolve_combines_and_weights_sources(conn):
    v = ledger.get_or_create_venue(conn, "Zebulon", "Los Angeles")
    s = ledger.find_or_create_show(conn, "2026-09-17", v)
    a = ledger.get_or_create_artist(conn, "Flat Worms")
    # A weak source says headliner, the manual entry says opener: manual wins.
    ledger.add_claim(conn, s, a, "llm", "x", role="headliner", confidence=0.6)
    ledger.add_claim(conn, s, a, "manual", "m", role="opener", confidence=1.0)
    b = conn.execute("SELECT * FROM billings WHERE show_id = ? AND artist_id = ?", (s, a)).fetchone()
    assert b["role"] == "opener"
    assert b["n_sources"] == 2
    assert 0 < b["confidence"] < 1


def test_agreeing_sources_raise_confidence(conn):
    v = ledger.get_or_create_venue(conn, "Zebulon", "Los Angeles")
    s = ledger.find_or_create_show(conn, "2026-09-17", v)
    a = ledger.get_or_create_artist(conn, "Flat Worms")
    ledger.add_claim(conn, s, a, "jsonld", "e1", role="opener", confidence=0.8)
    one = conn.execute("SELECT confidence FROM billings WHERE show_id = ?", (s,)).fetchone()[0]
    ledger.add_claim(conn, s, a, "setlistfm", "sl", role="opener", confidence=0.75)
    two = conn.execute("SELECT confidence FROM billings WHERE show_id = ?", (s,)).fetchone()[0]
    assert two > one


def test_unchanged_claim_reports_no_change(conn):
    v = ledger.get_or_create_venue(conn, "Zebulon", "Los Angeles")
    s = ledger.find_or_create_show(conn, "2026-09-17", v)
    a = ledger.get_or_create_artist(conn, "Flat Worms")
    assert ledger.add_claim(conn, s, a, "jsonld", "e1", role="opener", confidence=0.8)
    assert not ledger.add_claim(conn, s, a, "jsonld", "e1", role="opener", confidence=0.8)


def test_many_acts_marks_festival(conn):
    v = ledger.get_or_create_venue(conn, "Big Field", "Somewhere")
    s = ledger.find_or_create_show(conn, "2026-07-01", v)
    for i in range(ledger.FESTIVAL_MIN_ACTS):
        a = ledger.get_or_create_artist(conn, f"Band {i}")
        ledger.add_claim(conn, s, a, "jsonld", "fest", role="opener" if i else "headliner", confidence=0.8)
    assert conn.execute("SELECT is_festival FROM shows WHERE id = ?", (s,)).fetchone()[0] == 1
