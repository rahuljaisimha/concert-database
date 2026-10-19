import io
import tarfile

from concertdb import db, ledger, musicbrainz
from conftest import OSEES, add_graph_artist

CAMILLE_FR = "00000000-0000-0000-0000-00000000000c"
CAMILLE_US = "00000000-0000-0000-0000-00000000000d"
PRINCE = "00000000-0000-0000-0000-0000000000a1"


def _artist(rid, mbid, name, typ="2", comment="\\N"):
    # id, gid, name, sort_name, 6 date fields, type, area, gender, comment, edits, updated, ended, ...
    return [str(rid), mbid, name, name, *["\\N"] * 6, typ, "\\N", "\\N", comment, "0", "\\N", "f", "\\N", "\\N"]


def _dump(tmp_path, alias_first=True):
    artists = [_artist(1, OSEES, "Osees", comment="US garage rock band"),
               _artist(2, CAMILLE_FR, "Camille", "1", "French singer"),
               _artist(3, CAMILLE_US, "Camille", "1", "US rapper"),
               _artist(4, PRINCE, "Prince", "1")]
    aliases = [["10", "1", "Thee Oh Sees", "\\N", "0", "\\N", "1", "Oh Sees, Thee"],
               ["11", "1", "Oh Sees", "\\N", "0", "\\N", "1", "Oh Sees"],
               ["12", "4", "Camille", "\\N", "0", "\\N", "1", "Camille"]]
    tsv = lambda rows: "".join("\t".join(r) + "\n" for r in rows)   # noqa: E731
    path = tmp_path / "mbdump.tar.bz2"
    with tarfile.open(path, "w:bz2") as tar:
        members = [("mbdump/artist_alias", tsv(aliases)), ("mbdump/artist", tsv(artists))]
        for name, text in (members if alias_first else members[::-1]) + [("mbdump/release", "x\n")]:
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return str(path)


def _ledger_with_mb(tmp_path, alias_first=True):
    conn = db.connect(tmp_path / "ledger.sqlite")
    stats = musicbrainz.load(conn, tmp_path / "ledger.sqlite", _dump(tmp_path, alias_first), log=lambda *_: None)
    return conn, stats


def test_build_reads_both_tables_in_either_order(tmp_path):
    for alias_first in (True, False):
        conn, stats = _ledger_with_mb(tmp_path, alias_first)
        assert stats["artists"] == 4 and stats["aliases"] == 3
        names = {r[0] for r in conn.execute("SELECT name_norm FROM mb.names WHERE mbid = ?", (OSEES,))}
        assert names == {"osees", "thee oh sees", "oh sees"}                # no sort names
        conn.close()


def test_names_resolve_to_musicbrainz_artists(tmp_path):
    conn, _ = _ledger_with_mb(tmp_path)
    assert ledger.get_or_create_artist(conn, "Thee Oh Sees") == OSEES
    row = conn.execute("SELECT name, type, disambiguation FROM artists WHERE id = ?", (OSEES,)).fetchone()
    assert tuple(row) == ("Osees", "group", "US garage rock band")       # created from MusicBrainz
    # Three MusicBrainz artists answer to "Camille": not guessed...
    assert ledger.get_or_create_artist(conn, "Camille") == "name:camille"
    # ...unless exactly one of them is already in the ledger.
    ledger.get_or_create_artist(conn, "x", mbid=CAMILLE_FR)
    assert ledger.get_or_create_artist(conn, "Camille") == CAMILLE_FR
    assert conn.execute("SELECT disambiguation FROM artists WHERE id = ?", (CAMILLE_FR,)).fetchone()[0] == "French singer"
    assert ledger.get_or_create_artist(conn, "Unknown Band") == "name:unknown band"


def test_reconnecting_attaches_musicbrainz(tmp_path):
    conn, _ = _ledger_with_mb(tmp_path)
    conn.close()
    conn = db.connect(tmp_path / "ledger.sqlite")
    assert db.has_musicbrainz(conn) and ledger.get_or_create_artist(conn, "Oh Sees") == OSEES


def test_load_fills_in_notes_of_artists_already_in_the_ledger(tmp_path):
    conn = db.connect(tmp_path / "ledger.sqlite")
    add_graph_artist(conn, OSEES, "Osees")
    conn.commit()
    stats = musicbrainz.load(conn, tmp_path / "ledger.sqlite", _dump(tmp_path), log=lambda *_: None)
    assert stats["ledger_artists_updated"] == 1
    assert conn.execute("SELECT disambiguation FROM artists WHERE id = ?", (OSEES,)).fetchone()[0] == "US garage rock band"


def test_old_ledgers_get_the_new_column(tmp_path):
    import sqlite3
    p = tmp_path / "old.sqlite"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE artists (id TEXT PRIMARY KEY, name TEXT NOT NULL, name_norm TEXT NOT NULL, mbid TEXT,"
              " type TEXT, in_graph INTEGER NOT NULL DEFAULT 0, degree INTEGER NOT NULL DEFAULT 0) STRICT")
    c.commit()
    c.close()
    conn = db.connect(p)
    assert "disambiguation" in {r["name"] for r in conn.execute("PRAGMA table_info(artists)")}
