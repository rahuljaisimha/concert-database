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


FUZZ_US = "00000000-0000-0000-0000-0000000000f1"     # Ty Segall's garage rock band
FUZZ_OTHER = "00000000-0000-0000-0000-0000000000f2"


def _dump_with_fuzz(tmp_path):
    artists = [_artist(1, OSEES, "Osees", comment="US garage rock band"),
               _artist(5, FUZZ_US, "Fuzz", comment="US garage rock band with Ty Segall"),
               _artist(6, FUZZ_OTHER, "Fuzz", comment="Japanese noise rock")]
    aliases = [["10", "1", "Thee Oh Sees", "\\N", "0", "\\N", "1", "Oh Sees, Thee"],
               ["11", "1", "Oh Sees", "\\N", "0", "\\N", "1", "Oh Sees"],
               ["12", "1", "OCS", "\\N", "0", "\\N", "1", "OCS"],
               ["13", "5", "FUZZ", "\\N", "0", "\\N", "1", "FUZZ"]]
    tsv = lambda rows: "".join("\t".join(r) + "\n" for r in rows)   # noqa: E731
    path = tmp_path / "fuzz.tar.bz2"
    with tarfile.open(path, "w:bz2") as tar:
        for name, text in (("mbdump/artist", tsv(artists)), ("mbdump/artist_alias", tsv(aliases))):
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return str(path)


def test_one_band_under_several_names(tmp_path):
    """Thee Oh Sees, Oh Sees, OCS and Osees are one band: one artist, not four."""
    conn = db.connect(tmp_path / "ledger.sqlite")
    musicbrainz.load(conn, tmp_path / "ledger.sqlite", _dump_with_fuzz(tmp_path), log=lambda *_: None)
    v = ledger.get_or_create_venue(conn, "Teragram Ballroom", "Los Angeles")
    show = ledger.find_or_create_show(conn, "2015-12-16", v)
    ledger.add_claim(conn, show, ledger.get_or_create_artist(conn, "Osees", mbid=OSEES), "setlistfm", "s1")
    for i, name in enumerate(["Thee Oh Sees", "Oh Sees", "OCS", "osees"]):
        assert ledger.get_or_create_artist(conn, name) == OSEES
        ledger.add_claim(conn, show, ledger.get_or_create_artist(conn, name), "gemini", f"g{i}", role="headliner",
                         confidence=0.8)
    assert [r[0] for r in conn.execute("SELECT artist_id FROM billings WHERE show_id = ?", (show,))] == [OSEES]


def test_two_bands_with_one_name(tmp_path):
    """Two MusicBrainz artists are called Fuzz: a bare "Fuzz" isn't guessed, unless one
    of them is already in the ledger (e.g. from setlist.fm, which sends the MBID)."""
    conn = db.connect(tmp_path / "ledger.sqlite")
    musicbrainz.load(conn, tmp_path / "ledger.sqlite", _dump_with_fuzz(tmp_path), log=lambda *_: None)
    assert ledger.get_or_create_artist(conn, "Fuzz") == "name:fuzz"
    assert ledger.get_or_create_artist(conn, "Fuzz", mbid=FUZZ_US) == FUZZ_US
    assert ledger.get_or_create_artist(conn, "Fuzz") == FUZZ_US
    assert conn.execute("SELECT disambiguation FROM artists WHERE id = ?", (FUZZ_US,)).fetchone()[0] == \
        "US garage rock band with Ty Segall"


def test_default_eval_cases_are_the_two_cheap_ones():
    from concertdb import gemini_eval
    assert [c["name"] for c in gemini_eval.load_cases()] == ["ty-segall-gold-coast", "iguana-death-cult-zebulon"]
    gold = gemini_eval.load_cases(["ty-segall-gold-coast"])[0]["shows"]
    assert len(gold) == 1 and gold[0]["openers"] == ["Earth Tongue", "Speeding Vehicle"]
    assert len(gemini_eval.load_cases(everything=True)) == 8
