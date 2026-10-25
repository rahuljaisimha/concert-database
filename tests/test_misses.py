from concertdb import misses, seed, state
from concertdb.sources import setlistfm
from conftest import OSEES
from test_musicbrainz import CAMILLE_FR, PRINCE, _ledger_with_mb

QUIET = {"log": lambda *_: None}


def fetcher(rows):
    """fetch(after_id) over a fixed list of (id, query, source), recording what was asked."""
    calls = []

    def fetch(after):
        calls.append(after)
        return [r for r in rows if r[0] > after]
    fetch.calls = calls
    return fetch


def priority(conn, mbid):
    unit = state.get(conn, setlistfm.SOURCE, f"artist:{mbid}")
    return unit["priority"] if unit else None


def test_names_and_mbids_resolve_through_musicbrainz_names_and_aliases(tmp_path):
    conn, _ = _ledger_with_mb(tmp_path)
    assert misses.resolve(conn, "Thee Oh Sees") == (OSEES, "name")              # an old name (alias)
    assert misses.resolve(conn, f"  {PRINCE.upper()} ") == (PRINCE, "mbid")    # a dropdown pick
    assert misses.resolve(conn, "Camille") == (None, "ambiguous")               # three artists use it
    assert misses.resolve(conn, "Nobody At All") == (None, "not found")
    assert misses.resolve(conn, "00000000-0000-0000-0000-0000000000ff") == (None, "unknown MBID")


def test_misses_are_prioritized_once_and_the_position_is_kept(tmp_path):
    conn, _ = _ledger_with_mb(tmp_path)
    fetch = fetcher([(1, "Thee Oh Sees", "explore-not-found"), (2, "thee oh sees", "sqlite-missing"),
                     (3, CAMILLE_FR, "dropdown-musicbrainz-pick"), (4, "Camille", "sqlite-missing"),
                     (5, "Nobody At All", "explore-not-found")])
    stats = misses.prioritize_misses(conn, fetch, **QUIET)
    assert stats == {"misses": 5, "prioritized": 2, "already_queued": 0, "ambiguous": 1, "not_found": 1,
                     "deferred": 0}
    assert priority(conn, OSEES) == seed.USER_PRIORITY and priority(conn, CAMILLE_FR) == seed.USER_PRIORITY
    assert conn.execute("SELECT name FROM artists WHERE id = ?", (OSEES,)).fetchone()["name"] == "Osees"
    # Next run starts after the last miss read, and a miss for an artist already queued adds nothing.
    fetch2 = fetcher([(5, "x", "y"), (6, "Osees", "explore-not-found")])
    stats = misses.prioritize_misses(conn, fetch2, **QUIET)
    assert fetch2.calls == [5] and stats["prioritized"] == 0 and stats["already_queued"] == 1


def test_at_most_max_per_run_new_artists_and_the_rest_wait(tmp_path):
    conn, _ = _ledger_with_mb(tmp_path)
    rows = [(1, OSEES, "a"), (2, PRINCE, "a"), (3, CAMILLE_FR, "a")]
    stats = misses.prioritize_misses(conn, fetcher(rows), max_per_run=2, **QUIET)
    assert stats["prioritized"] == 2 and stats["deferred"] == 1
    assert priority(conn, CAMILLE_FR) is None
    stats = misses.prioritize_misses(conn, fetcher(rows), max_per_run=2, **QUIET)   # picks up where it stopped
    assert stats["misses"] == 1 and stats["prioritized"] == 1
    assert priority(conn, CAMILLE_FR) == seed.USER_PRIORITY


def test_without_musicbrainz_names_only_mbids_resolve(conn):
    assert misses.resolve(conn, OSEES) == (OSEES, "mbid")
    assert misses.resolve(conn, "Osees")[0] is None


def test_run_skips_the_step_without_a_login(monkeypatch, capsys, tmp_path):
    from concertdb import cli
    monkeypatch.delenv(misses.ENV, raising=False)
    conn, _ = _ledger_with_mb(tmp_path)
    cli._misses_step(conn)
    assert "skipped (no MSM_MISSES_DB_URL)" in capsys.readouterr().out


def test_a_failed_fetch_never_fails_the_run(monkeypatch, capsys, tmp_path):
    from concertdb import cli, runs
    monkeypatch.setenv(misses.ENV, "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.setattr(misses, "fetch_from_postgres", lambda url, after: (_ for _ in ()).throw(OSError("down")))
    conn, _ = _ledger_with_mb(tmp_path)
    cli._misses_step(conn)
    assert "search misses problem (the run carries on): OSError: down" in capsys.readouterr().out
    assert runs.recent(conn, step="misses")[0]["ok"] == 0
