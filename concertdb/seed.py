"""Import the artist universe from MusicSceneMap's graph.db(.gz).

Every person/group node becomes an artist (in_graph = 1) and a setlist.fm unit
of work. Groups are crawled before people, and better-connected artists first.
"""

import gzip
import shutil
import sqlite3
import tempfile
from pathlib import Path

from . import state
from .normalize import norm
from .sources import setlistfm

BAND_RELS = ("member_of", "former_member_of", "support_musician")
DEGREE_CAP = 12
GROUP_BONUS = 1_000
USER_PRIORITY = setlistfm.NO_CAP_PRIORITY   # also exempts the artist from the first-pass page cap


def _open_graph(path: Path) -> tuple[sqlite3.Connection, Path | None]:
    with open(path, "rb") as f:
        gz = f.read(2) == b"\x1f\x8b"
    if not gz:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True), None
    tmp = Path(tempfile.mkstemp(suffix=".db")[1])
    with gzip.open(path, "rb") as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return sqlite3.connect(str(tmp)), tmp


GRAPH_URL = "https://raw.githubusercontent.com/rahuljaisimha/MusicSceneMap/main/public/graph.db.gz"


def _download(url: str) -> Path:
    import requests

    fd, name = tempfile.mkstemp(suffix=".db.gz")
    with requests.get(url, stream=True, timeout=120) as r, open(fd, "wb") as f:
        r.raise_for_status()
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
    return Path(name)


def seed_from_graph(conn: sqlite3.Connection, graph_path: str | Path) -> int:
    """graph_path: a local graph.db(.gz) or an http(s) URL to one."""
    downloaded = None
    if str(graph_path).startswith(("http://", "https://")):
        downloaded = graph_path = _download(str(graph_path))
    try:
        return _seed(conn, Path(graph_path))
    finally:
        if downloaded:
            downloaded.unlink(missing_ok=True)


def _seed(conn: sqlite3.Connection, graph_path: Path) -> int:
    g, tmp = _open_graph(graph_path)
    try:
        degree = dict(g.execute(
            f"SELECT id, COUNT(*) FROM (SELECT source AS id FROM edges WHERE rel_type IN {BAND_RELS}"
            f" UNION ALL SELECT target FROM edges WHERE rel_type IN {BAND_RELS}) GROUP BY id"
        ).fetchall())
        rows = g.execute("SELECT id, name, type FROM nodes WHERE type IN ('person', 'group')").fetchall()
    finally:
        g.close()
        if tmp:
            tmp.unlink(missing_ok=True)

    for mbid, name, typ in rows:
        d = degree.get(mbid, 0)
        conn.execute(
            "INSERT INTO artists (id, name, name_norm, mbid, type, in_graph, degree)"
            " VALUES (?, ?, ?, ?, ?, 1, ?)"
            " ON CONFLICT (id) DO UPDATE SET name = excluded.name, name_norm = excluded.name_norm,"
            " type = excluded.type, in_graph = 1, degree = excluded.degree",
            (mbid, name, norm(name), mbid, typ, d),
        )
        # Groups play shows far more often than individual people; crawl them first.
        # Degree is capped: the most-connected nodes are collectives and charity
        # ensembles ("Les Enfoirés"), not touring bands.
        priority = 1 + min(d, DEGREE_CAP) + (GROUP_BONUS if typ == "group" else 0)
        state.ensure(conn, setlistfm.SOURCE, f"artist:{mbid}", priority=priority)
    conn.commit()
    return len(rows)


def prioritize(conn: sqlite3.Connection, names_or_ids: list[str]) -> list[str]:
    """Move named artists to the front of the setlist.fm queue. Returns unmatched names."""
    missing = []
    for n in names_or_ids:
        rows = conn.execute(
            "SELECT id FROM artists WHERE (id = ? OR name_norm = ?) AND mbid IS NOT NULL",
            (n, norm(n)),
        ).fetchall()
        if not rows:
            missing.append(n)
        for r in rows:
            state.ensure(conn, setlistfm.SOURCE, f"artist:{r['id']}", priority=USER_PRIORITY)
    conn.commit()
    return missing
