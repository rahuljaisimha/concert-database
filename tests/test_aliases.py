import io
import tarfile

from concertdb import aliases, ledger
from conftest import OSEES, add_graph_artist


def _dump(tmp_path, alias_first=True):
    """A tiny mbdump.tar.bz2 with the two tables, in the dump's COPY text format."""
    artist = "\t".join(["1", OSEES, "Osees", "Osees", "\\N"]) + "\n" + \
             "\t".join(["2", "00000000-0000-0000-0000-000000000002", "Someone Else", "Else, Someone", "\\N"]) + "\n" + \
             "\t".join(["3", "00000000-0000-0000-0000-000000000003", "Camille", "Camille", "\\N"]) + "\n"
    alias_rows = [["10", "1", "Thee Oh Sees", "\\N", "0", "\\N", "1", "Oh Sees, Thee"],
                  ["11", "1", "OCS", "\\N", "0", "\\N", "1", "OCS"],
                  ["12", "2", "Oh Sees", "\\N", "0", "\\N", "1", "Oh Sees"],
                  ["13", "1", "Camille", "\\N", "0", "\\N", "1", "Camille"]]
    alias = "".join("\t".join(r) + "\n" for r in alias_rows)
    path = tmp_path / "mbdump.tar.bz2"
    with tarfile.open(path, "w:bz2") as tar:
        members = [("mbdump/artist_alias", alias), ("mbdump/artist", artist)]
        for name, text in (members if alias_first else members[::-1]) + [("mbdump/release", "x\n")]:
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return str(path)


def test_read_dump_keeps_only_graph_artists(tmp_path):
    for alias_first in (True, False):
        names = aliases.read_dump(_dump(tmp_path, alias_first), {OSEES}, log=lambda *_: None)
        # Not "Oh Sees" (another artist's alias here), "Camille" (another artist's name), or sort names.
        assert names == {OSEES: {"Osees", "Thee Oh Sees", "OCS"}}


def test_names_match_graph_artists_through_aliases(conn, tmp_path):
    add_graph_artist(conn, OSEES, "Osees")
    add_graph_artist(conn, "g2", "Other Band")
    add_graph_artist(conn, "g3", "Third Band")
    aliases.store(conn, {OSEES: {"Osees", "Thee Oh Sees", "OCS"}, "g2": {"Other Band", "Shared"},
                         "g3": {"Third Band", "Shared"}})
    assert ledger.get_or_create_artist(conn, "Thee Oh Sees") == OSEES
    assert ledger.get_or_create_artist(conn, "THEE OH SEES") == OSEES
    assert ledger.get_or_create_artist(conn, "Shared") == "name:shared"     # two graph artists: not guessed
    assert ledger.get_or_create_artist(conn, "Other Band") == "g2"


def test_load_reads_the_dump(conn, tmp_path):
    add_graph_artist(conn, OSEES, "Osees")
    stats = aliases.load(conn, _dump(tmp_path), log=lambda *_: None)
    assert stats["artists"] == 1 and stats["aliases"] >= 3
    assert ledger.get_or_create_artist(conn, "OCS") == OSEES
