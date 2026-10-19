"""Every MusicBrainz artist's names, for matching sources that send only a name.

setlist.fm sends MusicBrainz IDs; Pollstar, Gemini and pages send names. This
builds data/musicbrainz.sqlite from MusicBrainz's full data export:

  artists  every artist: mbid, name, disambiguation note ("US garage rock
           band"), type
  names    each artist's name and aliases, normalized ("thee oh sees" ->
           Osees's mbid). Sort names ("Cooper, Alan") are left out: they
           collide with other artists' real names.

It sits next to the ledger and is attached to every connection as `mb`
(db.connect); it is derived data, rebuilt in a few minutes, so it isn't backed
up. ledger.get_or_create_artist() looks names up here (see "Matching names" in
docs/ARCHITECTURE.md). The export (mbdump.tar.bz2, ~7 GB) is read as a stream:
the artist and artist_alias tables come first in the archive, so it reads
~500 MB and stops (about a minute and a half); nothing big is written but
the result (~0.8 GB: 3M artists, 3.5M names).

  concertdb mb-load [--dump URL-or-path]
"""

import os
import sqlite3
import tarfile
from pathlib import Path

import requests

from . import db
from .normalize import norm

EXPORT = "https://data.metabrainz.org/pub/musicbrainz/data/fullexport"
DUMP_FILE = "mbdump.tar.bz2"
FILE = "musicbrainz.sqlite"
# Column positions in the dump (MusicBrainz schema, admin/sql/CreateTables.sql).
ARTIST_COLS = {"id": 0, "gid": 1, "name": 2, "type": 10, "comment": 13}
ALIAS_COLS = {"artist": 1, "name": 2}
TYPES = {"1": "person", "2": "group", "5": "group", "6": "group"}   # person, group, orchestra, choir
SCHEMA = """
CREATE TABLE artists (
    rid     INTEGER PRIMARY KEY,         -- MusicBrainz's row id (aliases refer to it)
    mbid    TEXT NOT NULL UNIQUE,
    name    TEXT NOT NULL,
    comment TEXT,                        -- the disambiguation note
    type    TEXT
);
CREATE TABLE names (
    name_norm TEXT NOT NULL,
    mbid      TEXT NOT NULL,
    PRIMARY KEY (name_norm, mbid)
) WITHOUT ROWID;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""
UA = {"User-Agent": "concert-database/0.1 (+https://github.com/rahuljaisimha/concert-database)"}


def path_for(ledger_path) -> Path:
    return Path(ledger_path).with_name(FILE)


def latest_dump_url() -> str:
    r = requests.get(f"{EXPORT}/LATEST", timeout=30, headers=UA)
    r.raise_for_status()
    return f"{EXPORT}/{r.text.strip()}/{DUMP_FILE}"


def _open(dump: str):
    if dump.startswith(("http://", "https://")):
        r = requests.get(dump, stream=True, timeout=120, headers=UA)
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
    """Rows of a dump table (PostgreSQL COPY text format; \\N is NULL)."""
    for line in _lines(member_file):
        yield [None if f == "\\N" else f.replace("\\t", "\t").replace("\\n", "\n").replace("\\\\", "\\")
               for f in line.decode("utf-8", "replace").split("\t")]


def build(dump: str, out: Path, log=print) -> dict:
    """Write out (atomically: a .tmp file, then renamed)."""
    tmp = out.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    m = sqlite3.connect(tmp)
    m.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;" + SCHEMA)
    m.execute("CREATE TEMP TABLE aliases (rid INTEGER, name_norm TEXT)")
    seen, counts = set(), {"artists": 0, "aliases": 0}
    with _open(dump) as f, tarfile.open(fileobj=f, mode="r|bz2") as tar:
        for member in tar:
            base = member.name.rsplit("/", 1)[-1]
            if base not in ("artist", "artist_alias") or member.name.count("/") != 1:
                continue
            log(f"  reading {member.name} ({member.size / 1e6:.0f} MB)")
            rows = _rows(tar.extractfile(member))
            if base == "artist":
                batch = []
                for r in rows:
                    batch.append((int(r[ARTIST_COLS["id"]]), r[ARTIST_COLS["gid"]], r[ARTIST_COLS["name"]],
                                  r[ARTIST_COLS["comment"]] or None, TYPES.get(r[ARTIST_COLS["type"]] or "")))
                    if len(batch) >= 50_000:
                        _add_artists(m, batch)
                        counts["artists"] += len(batch)
                        batch = []
                _add_artists(m, batch)
                counts["artists"] += len(batch)
            else:
                batch = []
                for r in rows:
                    batch.append((int(r[ALIAS_COLS["artist"]]), norm(r[ALIAS_COLS["name"]])))
                    if len(batch) >= 50_000:
                        m.executemany("INSERT INTO aliases VALUES (?, ?)", batch)
                        counts["aliases"] += len(batch)
                        batch = []
                m.executemany("INSERT INTO aliases VALUES (?, ?)", batch)
                counts["aliases"] += len(batch)
            seen.add(base)
            if seen == {"artist", "artist_alias"}:
                break                     # the rest of the 7 GB isn't needed
    if seen != {"artist", "artist_alias"}:
        m.close()
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"dump has no {({'artist', 'artist_alias'} - seen)} table")
    m.execute("INSERT OR IGNORE INTO names SELECT al.name_norm, a.mbid FROM aliases al"
              " JOIN artists a ON a.rid = al.rid WHERE al.name_norm != ''")
    m.executemany("INSERT INTO meta VALUES (?, ?)", [("dump", dump)])
    counts["names"] = m.execute("SELECT COUNT(*) FROM names").fetchone()[0]
    m.commit()
    m.close()
    os.replace(tmp, out)
    return counts


def _add_artists(m, batch) -> None:
    m.executemany("INSERT INTO artists VALUES (?, ?, ?, ?, ?)", batch)
    m.executemany("INSERT OR IGNORE INTO names VALUES (?, ?)",
                  [(norm(name), mbid) for _, mbid, name, _, _ in batch if norm(name)])


def load(conn: sqlite3.Connection, ledger_path, dump: str | None = None, log=print) -> dict:
    """Build musicbrainz.sqlite next to the ledger, attach it, and fill in the
    disambiguation note (and missing type) of artists already in the ledger."""
    dump = dump or latest_dump_url()
    out = path_for(ledger_path)
    log(f"reading MusicBrainz artists from {dump}")
    stats = build(dump, out, log)
    if db.has_musicbrainz(conn):
        conn.commit()
        conn.execute("DETACH DATABASE mb")      # the old file, replaced by build()
    db.attach_musicbrainz(conn, out)
    db.begin_write(conn)
    stats["ledger_artists_updated"] = conn.execute(
        "UPDATE artists SET"
        " disambiguation = (SELECT comment FROM mb.artists m WHERE m.mbid = artists.mbid),"
        " type = COALESCE(type, (SELECT type FROM mb.artists m WHERE m.mbid = artists.mbid))"
        " WHERE mbid IS NOT NULL AND mbid IN (SELECT mbid FROM mb.artists)").rowcount
    conn.commit()
    stats["size_mb"] = round(out.stat().st_size / 1e6)
    return stats
