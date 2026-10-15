import gzip
import sqlite3

from concertdb import backup, cli, db, seed, state


def test_snapshot_round_trip(conn, tmp_path):
    conn.execute("INSERT INTO venues (name, name_norm) VALUES ('Zebulon', 'zebulon')")
    conn.commit()
    snap = backup.snapshot(conn, tmp_path)
    restored = tmp_path / "restored.sqlite"
    with gzip.open(snap, "rb") as src:
        restored.write_bytes(src.read())
    r = sqlite3.connect(restored)
    assert r.execute("SELECT name FROM venues").fetchone()[0] == "Zebulon"


def test_remote_retention():
    keys = [f"snapshots/ledger-202609{d:02d}T030000Z.sqlite.gz" for d in range(1, 31)]
    keys += [f"snapshots/ledger-202606{d:02d}T030000Z.sqlite.gz" for d in (1, 8, 15)]
    keep = backup.keep_remote(keys)
    assert all(f"snapshots/ledger-202609{d:02d}T030000Z.sqlite.gz" in keep for d in range(24, 31))
    assert len(keep) <= backup.KEEP_DAILY + backup.KEEP_WEEKLY
    assert "snapshots/ledger-20260602T030000Z.sqlite.gz" not in keep


def test_seed_from_gzipped_graph(conn, tmp_path):
    g = tmp_path / "graph.db"
    gc = sqlite3.connect(g)
    gc.executescript("""
        CREATE TABLE nodes (id TEXT, name TEXT, type TEXT);
        CREATE TABLE edges (source TEXT, target TEXT, rel_type TEXT);
        INSERT INTO nodes VALUES ('p1', 'Tim Hellman', 'person'), ('g1', 'Osees', 'group'),
                                 ('g2', 'Flat Worms', 'group'), ('a1', 'Some Album', 'album');
        INSERT INTO edges VALUES ('p1', 'g1', 'member_of'), ('p1', 'g2', 'member_of'),
                                 ('p1', 'a1', 'instrument');
    """)
    gc.commit()
    gc.close()
    gz = tmp_path / "graph.db.gz"
    gz.write_bytes(gzip.compress(g.read_bytes()))

    assert seed.seed_from_graph(conn, gz) == 3
    due = [r["key"] for r in state.due(conn, "setlistfm")]
    assert due[-1] == "artist:p1"          # people after groups
    assert set(due[:2]) == {"artist:g1", "artist:g2"}
    assert conn.execute("SELECT degree FROM artists WHERE id = 'p1'").fetchone()[0] == 2


def test_cli_add_show_and_export(tmp_path, capsys):
    dbp = str(tmp_path / "ledger.sqlite")
    cli.main(["--db", dbp, "add-show", "--date", "2026-09-17", "--venue", "Zebulon",
              "--city", "Los Angeles", "--headliner", "Iguana Death Cult", "--opener", "Flat Worms"])
    out = capsys.readouterr().out
    assert "headliner Iguana Death Cult" in out and "opener    Flat Worms" in out
    cli.main(["--db", dbp, "shows", "Flat Worms"])
    assert "Zebulon" in capsys.readouterr().out
    csv = tmp_path / "e.csv"
    cli.main(["--db", dbp, "export", "--out", str(csv)])
    assert "Flat Worms" in csv.read_text()
    cli.main(["--db", dbp, "status"])
    assert "Shows:" in capsys.readouterr().out
    cli.main(["--db", dbp, "backup"])
    assert list((tmp_path / "backups").glob("ledger-*.sqlite.gz"))


def test_prioritize(conn, tmp_path):
    from concertdb.normalize import norm
    for mbid, name in (("g1", "Osees"), ("g2", "Flat Worms")):
        conn.execute("INSERT INTO artists (id, name, name_norm, mbid, type, in_graph) VALUES (?, ?, ?, ?, 'group', 1)",
                     (mbid, name, norm(name), mbid))
        state.ensure(conn, "setlistfm", f"artist:{mbid}", priority=5)
    assert seed.prioritize(conn, ["flat worms", "Nobody"]) == ["Nobody"]
    assert state.due(conn, "setlistfm")[0]["key"] == "artist:g2"
