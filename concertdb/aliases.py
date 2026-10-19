"""Other names of graph artists, from MusicBrainz's data dump.

Sources that send only a name (Pollstar, Gemini, pages) are matched to graph
artists by name. The graph keeps one name per artist, the current one
("Osees"), so older names ("Thee Oh Sees", "Oh Sees", "OCS") used to become
separate unmatched artists. This loads graph artists' MusicBrainz names and
aliases into artist_aliases, keeping only names no other MusicBrainz artist
has; ledger.get_or_create_artist() uses them when a name matches no graph
artist directly.

Source: the full export's mbdump.tar.bz2 (~7 GB), read as a stream: only the
artist and artist_alias tables are kept, and only for graph artists, so nothing
big is written to disk. Run it again to refresh (it replaces the table).

  concertdb mb-aliases [--dump URL-or-path]
"""

import sqlite3
import tarfile
from pathlib import Path

import requests

from . import db
from .normalize import norm

EXPORT = "https://data.metabrainz.org/pub/musicbrainz/data/fullexport"
DUMP_FILE = "mbdump.tar.bz2"
# Column order of the two tables in the dump (MusicBrainz schema, admin/sql/CreateTables.sql).
ARTIST_COLS = {"id": 0, "gid": 1, "name": 2}
ALIAS_COLS = {"artist": 1, "name": 2}


def latest_dump_url() -> str:
    r = requests.get(f"{EXPORT}/LATEST", timeout=30)
    r.raise_for_status()
    return f"{EXPORT}/{r.text.strip()}/{DUMP_FILE}"


def _open(dump: str):
    """A file object over the dump, streamed when it is a URL."""
    if dump.startswith(("http://", "https://")):
        r = requests.get(dump, stream=True, timeout=120,
                         headers={"User-Agent": "concert-database/0.1 (+https://github.com/rahuljaisimha/concert-database)"})
        r.raise_for_status()
        r.raw.decode_content = True
        return r.raw
    return open(Path(dump), "rb")


def _lines(member_file, chunk=1 << 20):
    """Lines of a tar member read as a stream (it can't seek, so no TextIOWrapper)."""
    rest = b""
    while True:
        block = member_file.read(chunk)
        if not block:
            break
        *lines, rest = (rest + block).split(b"\n")
        yield from lines
    if rest:
        yield rest


def _rows(member_file):
    """TSV rows of a dump table (PostgreSQL COPY text format)."""
    for line in _lines(member_file):
        yield [f.replace("\\t", "\t").replace("\\n", "\n").replace("\\\\", "\\")
               for f in line.decode("utf-8", "replace").split("\t")]


def read_dump(dump: str, wanted_mbids: set[str], log=print) -> dict[str, set[str]]:
    """{mbid: {names}} for the wanted artists: their MusicBrainz name and aliases,
    keeping only names that no OTHER MusicBrainz artist has (as name or alias).
    Being unique among graph artists isn't enough: "Camille" is an alias of
    Prince, but other artists are called Camille, and the one on a bill is
    rarely Prince. Sort names ("Cooper, Alan") are left out for the same reason."""
    ids: dict[str, str] = {}              # MusicBrainz row id -> mbid, wanted artists only
    owner: dict[str, str] = {}            # normalized name -> the one artist row id with it, "*" if several
    aliases_seen: list[tuple[str, str]] = []
    primary: list[tuple[str, str]] = []

    def own(name: str, row_id: str) -> None:
        k = norm(name)
        if k:
            prev = owner.get(k)
            owner[k] = row_id if prev in (None, row_id) else "*"

    seen = set()
    with _open(dump) as f, tarfile.open(fileobj=f, mode="r|bz2") as tar:
        for m in tar:
            base = m.name.rsplit("/", 1)[-1]
            if base not in ("artist", "artist_alias") or m.name.count("/") != 1:
                continue
            log(f"  reading {m.name} ({m.size / 1e6:.0f} MB)")
            fh = tar.extractfile(m)
            if base == "artist":
                for row in _rows(fh):
                    rid, name = row[ARTIST_COLS["id"]], row[ARTIST_COLS["name"]]
                    own(name, rid)
                    if row[ARTIST_COLS["gid"]] in wanted_mbids:
                        ids[rid] = row[ARTIST_COLS["gid"]]
                        primary.append((rid, name))
            else:
                for row in _rows(fh):
                    rid, name = row[ALIAS_COLS["artist"]], row[ALIAS_COLS["name"]]
                    own(name, rid)
                    aliases_seen.append((rid, name))
            seen.add(base)
            if seen == {"artist", "artist_alias"}:
                break                     # the rest of the 7 GB isn't needed
    if seen != {"artist", "artist_alias"}:
        raise RuntimeError(f"dump has no {({'artist', 'artist_alias'} - seen)} table")
    names: dict[str, set[str]] = {}
    for rid, name in primary + aliases_seen:
        if rid in ids and owner.get(norm(name)) == rid:
            names.setdefault(ids[rid], set()).add(name)
    return names


def store(conn: sqlite3.Connection, names: dict[str, set[str]]) -> dict:
    """Replace artist_aliases."""
    db.begin_write(conn)
    conn.execute("DELETE FROM artist_aliases")
    n = 0
    for mbid, ns in names.items():
        for name in ns:
            key = norm(name)
            if key and name != "\\N":
                conn.execute("INSERT OR IGNORE INTO artist_aliases (alias_norm, artist_id, alias)"
                             " VALUES (?, ?, ?)", (key, mbid, name))
                n += 1
    conn.commit()
    unique = conn.execute("SELECT COUNT(*) FROM (SELECT alias_norm FROM artist_aliases"
                          " GROUP BY alias_norm HAVING COUNT(DISTINCT artist_id) = 1)").fetchone()[0]
    return {"artists": len(names), "aliases": n, "unambiguous_names": unique}


def load(conn: sqlite3.Connection, dump: str | None = None, log=print) -> dict:
    wanted = {r[0] for r in conn.execute("SELECT id FROM artists WHERE in_graph = 1 AND mbid IS NOT NULL")}
    dump = dump or latest_dump_url()
    log(f"reading aliases for {len(wanted):,} graph artists from {dump}")
    return store(conn, read_dump(dump, wanted, log))
