"""Derived output for MusicSceneMap: opened_for edges (opener → headliner)."""

import csv
import sqlite3
from pathlib import Path

QUERY = """
SELECT o.artist_id AS opener_id, oa.name AS opener_name,
       h.artist_id AS headliner_id, ha.name AS headliner_name,
       COUNT(*) AS shows,
       ROUND(MAX(MIN(o.confidence, h.confidence)), 3) AS confidence,
       MIN(s.date) AS first_date, MAX(s.date) AS last_date
FROM billings o
JOIN billings h ON h.show_id = o.show_id AND h.role = 'headliner'
JOIN shows s ON s.id = o.show_id
JOIN artists oa ON oa.id = o.artist_id
JOIN artists ha ON ha.id = h.artist_id
WHERE o.role = 'opener'
  AND s.is_festival = 0
  AND s.status NOT IN ('cancelled', 'postponed')
  AND o.confidence >= :min_conf AND h.confidence >= :min_conf
  AND (:known_only = 0 OR (oa.mbid IS NOT NULL AND ha.mbid IS NOT NULL))
GROUP BY o.artist_id, h.artist_id
ORDER BY shows DESC, opener_name
"""


def opened_for(conn: sqlite3.Connection, min_conf: float = 0.4, known_only: bool = False) -> list[sqlite3.Row]:
    """known_only: both acts are MusicBrainz artists (not unmatched 'name:' ones)."""
    return conn.execute(QUERY, {"min_conf": min_conf, "known_only": int(known_only)}).fetchall()


def write_csv(rows: list[sqlite3.Row], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        cols = ["opener_id", "opener_name", "headliner_id", "headliner_name",
                "shows", "confidence", "first_date", "last_date"]
        w.writerow(cols)
        for r in rows:
            w.writerow([r[c] for c in cols])
