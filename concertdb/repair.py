"""Split shows that the old matching rule merged wrongly.

Until this change, any two records at a venue within a day of each other
became one show, so a venue's shows on consecutive nights (two Pollstar
events, a residency) were merged into one bill. find_or_create_show now keeps
them apart; repair_shows() fixes shows stored before that.

Each claim's own date comes from claims.extra["date"] (stored since this
change), else from the raw response archive (data/raw/<source>/), else from a
manual entry's native_id, else the show's date. For every show with records
that the current rules would not put together, the largest group stays and
each other record is placed again with ledger.find_or_create_show, which
reuses a matching show nearby or creates a new one.

Safe to run more than once: a ledger with nothing to split is left unchanged.
"""

import gzip
import json
import sqlite3
from datetime import date
from pathlib import Path

from . import db, ledger
from .sources import pollstar, setlistfm


def archived_dates(data_dir: Path) -> dict[tuple[str, str], str]:
    """(source, native_id) → event date, from the raw response archive."""
    out: dict[tuple[str, str], str] = {}
    for source in ("setlistfm", "pollstar"):
        for f in sorted((Path(data_dir) / "raw" / source).glob("*.jsonl.gz")):
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                        body = json.loads(rec["body"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    if not isinstance(body, dict):
                        continue
                    if source == "setlistfm":
                        for sl in body.get("setlist") or []:
                            try:
                                out[(source, sl["id"])] = setlistfm.parse_date(sl["eventDate"])
                            except (KeyError, ValueError, TypeError):
                                pass
                    elif "/events" in rec.get("url", ""):
                        try:
                            for e in pollstar.parse_events(body):
                                out[(source, e["id"])] = e["date"]
                        except (KeyError, ValueError, TypeError):
                            pass
    return out


def _claim_date(c: sqlite3.Row, dates: dict) -> str | None:
    d = json.loads(c["extra"] or "{}").get("date")
    if d:
        return d
    d = dates.get((c["source"], c["native_id"]))
    if d:
        return d
    if c["source"] == "manual" and c["native_id"].startswith("manual:"):
        return c["native_id"].split(":")[1]
    return None


def _events(claims: list[sqlite3.Row], show_date: str, dates: dict) -> list[dict]:
    """The show's records grouped by (source, native_id), each with its date and acts."""
    events: dict[tuple[str, str], dict] = {}
    for c in claims:
        ev = events.setdefault((c["source"], c["native_id"]), {
            "source": c["source"], "native_id": c["native_id"], "artists": set(), "claims": 0,
            "date": _claim_date(c, dates) or show_date,
        })
        ev["artists"].add(c["artist_id"])
        ev["claims"] += 1
    return list(events.values())


def _fits(group: list[dict], ev: dict) -> bool:
    """Would the current rules put this record in a show holding these records?"""
    claims = [{"artist_id": a, "source": g["source"], "native_id": g["native_id"]}
              for g in group for a in g["artists"]]
    if ledger._other_event(claims, ev["artists"], ev["source"], ev["native_id"]):
        return False
    gdate = group[0]["date"]
    if ev["date"] == gdate:
        return True
    near = abs((date.fromisoformat(ev["date"]) - date.fromisoformat(gdate)).days) <= 1
    return near and bool(ev["artists"] & {a for g in group for a in g["artists"]})


def _groups(events: list[dict], show_date: str) -> list[list[dict]]:
    # Records dated on the show's own date first, biggest first: they anchor the group that stays.
    order = sorted(events, key=lambda e: (e["date"] != show_date, -e["claims"]))
    groups: list[list[dict]] = []
    for ev in order:
        for g in groups:
            if _fits(g, ev):
                g.append(ev)
                break
        else:
            groups.append([ev])
    return groups


def repair_shows(conn: sqlite3.Connection, data_dir: Path, dry_run: bool = False, log=print) -> dict:
    dates = archived_dates(data_dir)
    stats = {"dates_from_archive": len(dates), "shows_checked": 0, "shows_split": 0, "records_moved": 0}
    db.begin_write(conn)
    stats["shows_before"] = conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0]
    touched: set[int] = set()
    examples = 0
    for sh in conn.execute("SELECT id, date, venue_id FROM shows ORDER BY id").fetchall():
        claims = conn.execute("SELECT * FROM claims WHERE show_id = ?", (sh["id"],)).fetchall()
        if len({(c["source"], c["native_id"]) for c in claims}) < 2:
            continue
        stats["shows_checked"] += 1
        events = _events(claims, sh["date"], dates)
        groups = _groups(events, sh["date"])
        if len(groups) < 2:
            continue
        stats["shows_split"] += 1
        touched.add(sh["id"])
        if examples < 10:
            examples += 1
            v = conn.execute("SELECT name, city FROM venues WHERE id = ?", (sh["venue_id"],)).fetchone()
            names = lambda g: ", ".join(sorted(
                conn.execute("SELECT name FROM artists WHERE id = ?", (a,)).fetchone()[0]
                for e in g for a in e["artists"]))
            log(f"  {sh['date']} {v['name']}, {v['city']}: keeps [{names(groups[0])}]; moves "
                + "; ".join(f"{g[0]['date']} [{names(g)}]" for g in groups[1:]))
        for g in groups[1:]:
            for ev in g:
                new = ledger.find_or_create_show(conn, ev["date"], sh["venue_id"], ev["artists"],
                                                 source=ev["source"], native_id=ev["native_id"],
                                                 exclude_show=sh["id"])
                conn.execute("UPDATE claims SET show_id = ? WHERE show_id = ? AND source = ? AND native_id = ?",
                             (new, sh["id"], ev["source"], ev["native_id"]))
                stats["records_moved"] += 1
                touched.add(new)
    for show_id in sorted(touched):
        if ledger.drop_show_if_empty(conn, show_id):
            continue
        if conn.execute("SELECT 1 FROM claims WHERE show_id = ? AND source = ? LIMIT 1",
                        (show_id, setlistfm.SOURCE)).fetchone():
            setlistfm.reinfer_show(conn, show_id)   # setlist.fm roles depend on who else played
        else:
            ledger.resolve_show(conn, show_id)
    # Shows left without any record (e.g. by an earlier move) carry no information.
    empty = [r[0] for r in conn.execute(
        "SELECT id FROM shows s WHERE NOT EXISTS (SELECT 1 FROM claims c WHERE c.show_id = s.id)")]
    for show_id in empty:
        ledger.drop_show_if_empty(conn, show_id)
    stats["empty_shows_removed"] = len(empty)
    stats["shows_after"] = conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0]
    if dry_run:
        conn.rollback()
        log("dry run: nothing saved")
    else:
        conn.commit()
    return stats
