from concertdb import export, ledger
from conftest import FLAT_WORMS, OSEES, add_graph_artist


def _show(conn, date, venue, acts, source="manual", conf=1.0):
    v = ledger.get_or_create_venue(conn, venue, "Los Angeles")
    s = ledger.find_or_create_show(conn, date, v)
    for i, name in enumerate(acts):
        a = ledger.get_or_create_artist(conn, name)
        ledger.add_claim(conn, s, a, source, f"{source}:{date}:{venue}", role="headliner" if i == 0 else "opener",
                         confidence=conf, position=i)
    return s


def test_opened_for_edges(conn):
    add_graph_artist(conn, OSEES, "Osees")
    add_graph_artist(conn, FLAT_WORMS, "Flat Worms")
    _show(conn, "2026-09-17", "Zebulon", ["Iguana Death Cult", "Flat Worms"])
    _show(conn, "2025-04-12", "The Echo", ["Osees", "Flat Worms"])
    _show(conn, "2025-06-01", "Teragram", ["Osees", "Flat Worms"])
    rows = export.opened_for(conn)
    edges = {(r["opener_name"], r["headliner_name"]): r["shows"] for r in rows}
    assert edges == {("Flat Worms", "Osees"): 2, ("Flat Worms", "Iguana Death Cult"): 1}
    known = export.opened_for(conn, known_only=True)
    assert [(r["opener_id"], r["headliner_id"]) for r in known] == [(FLAT_WORMS, OSEES)]


def test_cancelled_and_low_confidence_excluded(conn):
    s = _show(conn, "2026-10-05", "Zebulon", ["A", "B"])
    ledger.set_show_status(conn, s, "cancelled")
    _show(conn, "2026-10-06", "Zebulon", ["C", "D"], source="llm", conf=0.3)
    assert export.opened_for(conn) == []


def test_write_csv(conn, tmp_path):
    _show(conn, "2026-09-17", "Zebulon", ["Iguana Death Cult", "Flat Worms"])
    out = tmp_path / "e.csv"
    export.write_csv(export.opened_for(conn), out)
    lines = out.read_text().splitlines()
    assert lines[0].startswith("opener_id,opener_name")
    assert "Flat Worms" in lines[1]
