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


def test_smoke_run_with_fake_sources(tmp_path, monkeypatch):
    import json

    from concertdb import smoke
    from concertdb.sources import pollstar, setlistfm
    from conftest import fixture_text, ok

    g = tmp_path / "graph.db"
    gc = sqlite3.connect(g)
    gc.executescript("CREATE TABLE nodes (id TEXT, name TEXT, type TEXT);"
                     "CREATE TABLE edges (source TEXT, target TEXT, rel_type TEXT);")
    gc.executemany("INSERT INTO nodes VALUES (?, ?, 'group')",
                   [("5f647289-9ff8-494d-a349-7a388307ddde", "Flat Worms")] +
                   [(f"x{i}", f"Band {i}") for i in range(1500)])
    gc.commit()
    gc.close()

    calls = []

    class FakeSetlist:
        def __init__(self, *a):
            pass

        def page(self, kind, ident, page):
            calls.append(ident)
            return ok(fixture_text("setlistfm_venue_night.json"))

    class FakePollstar:
        def __init__(self, *a):
            pass

        def autocomplete(self, q):
            return ok(fixture_text("pollstar_autocomplete_zebulon.json"))

        def events(self, pid, page):
            return ok(fixture_text("pollstar_events_zebulon.json"))

    class FakeGemini:
        model = "gemini-3.8-flash"

        def __init__(self, *a, **k):
            pass

        def generate(self, model, prompt):
            assert "Iguana Death Cult" in prompt
            return ok(fixture_text("gemini_generate_zebulon.json"))

        def page(self, url):
            return ok(fixture_text("event_page.html"))

    from concertdb.sources import gemini
    monkeypatch.setattr(setlistfm, "Client", FakeSetlist)
    monkeypatch.setattr(pollstar, "Client", FakePollstar)
    monkeypatch.setattr(gemini, "Client", FakeGemini)
    monkeypatch.setenv("SETLISTFM_API_KEY", "test")
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    assert smoke.run(tmp_path / "smoke", str(g), log=lambda *_: None)
    assert calls[0] == "5f647289-9ff8-494d-a349-7a388307ddde"   # the test artist, not 1,500 others
    assert "x1" not in calls
    assert len(calls) <= smoke.SETLISTFM_REQUESTS


def test_locks_are_per_process_kind(tmp_path):
    import argparse
    args = argparse.Namespace(db=str(tmp_path / "ledger.sqlite"))
    crawl = cli._lock(args, "crawl")
    pollstar_lock = cli._lock(args, "pollstar", exit_if_busy=False)
    assert pollstar_lock is not None                                # runs next to the daily crawl
    assert cli._lock(args, "pollstar", exit_if_busy=False) is None  # but only one Pollstar process
    pollstar_lock.close()
    assert cli._lock(args, "pollstar", exit_if_busy=False) is not None
    crawl.close()


def test_run_skips_pollstar_while_its_process_runs(tmp_path, capsys, monkeypatch):
    import argparse
    monkeypatch.delenv("SETLISTFM_API_KEY", raising=False)
    dbp = str(tmp_path / "ledger.sqlite")
    held = cli._lock(argparse.Namespace(db=dbp), "pollstar")
    cli.main(["--db", dbp, "run", "--no-backup"])
    assert "Pollstar skipped (the separate Pollstar process is running)" in capsys.readouterr().out
    held.close()


def test_gemini_has_its_own_lock(tmp_path, monkeypatch):
    import pytest
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    dbp = tmp_path / "ledger.sqlite"
    with pytest.raises(SystemExit, match="GEMINI_API_KEY"):
        cli.main(["--db", str(dbp), "gemini"])
    assert (tmp_path / "ledger.gemini.lock").exists()    # not ledger.lock: runs alongside `concertdb run`
    assert not (tmp_path / "ledger.crawl.lock").exists()


def test_gemini_max_acts_from_env(monkeypatch):
    monkeypatch.setenv("GEMINI_MAX_ACTS", "3")
    assert cli.build_parser().parse_args(["gemini"]).max_acts == 3
    monkeypatch.delenv("GEMINI_MAX_ACTS")
    assert cli.build_parser().parse_args(["gemini"]).max_acts == 1
    assert cli.build_parser().parse_args(["gemini", "--max-acts", "2"]).max_acts == 2


def test_prune_raw_keeps_30_days(tmp_path):
    import json
    from datetime import date
    raw = tmp_path / "raw" / "pollstar"
    raw.mkdir(parents=True)
    for day in ("2026-08-01", "2026-09-17", "2026-09-18", "2026-10-17"):
        (raw / f"{day}.jsonl.gz").write_bytes(b"x")
    (raw / "notes.jsonl.gz").write_bytes(b"x")          # not a day file: left alone
    (tmp_path / "backups").mkdir()
    manifest = tmp_path / "backups" / "uploaded.json"
    manifest.write_text(json.dumps({"raw/pollstar/2026-08-01.jsonl.gz": 1, "raw/pollstar/2026-10-17.jsonl.gz": 1}))
    n = backup.prune_raw(tmp_path, 30, today=date(2026, 10, 18), log=lambda *_: None)
    assert n == 2
    assert sorted(p.name for p in raw.iterdir()) == ["2026-09-18.jsonl.gz", "2026-10-17.jsonl.gz", "notes.jsonl.gz"]
    assert json.loads(manifest.read_text()) == {"raw/pollstar/2026-10-17.jsonl.gz": 1}
    assert backup.prune_raw(tmp_path, 0, today=date(2027, 1, 1), log=lambda *_: None) == 0   # 0 keeps all
