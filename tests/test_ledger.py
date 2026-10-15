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


def test_show_dates_within_a_day_merge(conn):
    v = ledger.get_or_create_venue(conn, "Zebulon", "Los Angeles")
    s = ledger.find_or_create_show(conn, "2026-09-17", v)
    assert ledger.find_or_create_show(conn, "2026-09-18", v) == s   # past midnight
    assert ledger.find_or_create_show(conn, "2026-09-17", v) == s
    assert ledger.find_or_create_show(conn, "2026-09-20", v) != s


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
