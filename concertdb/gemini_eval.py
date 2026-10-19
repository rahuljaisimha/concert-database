"""Live check of the search + Gemini step on shows whose bills we know
(concertdb/gemini_cases.json). Each case runs in a throwaway database under
data/eval/, with real searches, page fetches and Gemini calls (~1-3 per case),
and is scored against the known bill. By default only the cases marked
"default" run (two shows, ~2 searches and 2 Gemini calls); --all runs every one.

  concertdb gemini-eval [--case NAME ...] [--all]
"""

import json
import shutil
from pathlib import Path

from . import db, ledger, musicbrainz, state
from .normalize import norm
from .sources import gemini

CASES = Path(__file__).with_name("gemini_cases.json")


def load_cases(names=None, everything: bool = False) -> list[dict]:
    """The named cases; else all (everything) or the ones marked default."""
    cases = json.loads(CASES.read_text())["cases"]
    if names:
        return [c for c in cases if c["name"] in names]
    return [c for c in cases if everything or c.get("default")]


def setup(conn, case: dict) -> dict[str, int]:
    """The case's artist (in the graph) and its shows, each with only that act listed,
    as setlist.fm would have them. Returns {date: show id}."""
    aid = case.get("mbid") or f"eval:{norm(case['artist'])}"
    conn.execute("INSERT OR IGNORE INTO artists (id, name, name_norm, mbid) VALUES (?, ?, ?, ?)",
                 (aid, case["artist"], norm(case["artist"]), aid))
    ids = {}
    for s in case["shows"]:
        v = ledger.get_or_create_venue(conn, s["venue"], s["city"], country=case.get("country"))
        ids[s["date"]] = ledger.find_or_create_show(conn, s["date"], v)
        ledger.add_claim(conn, ids[s["date"]], aid, "setlistfm", f"eval-{s['date']}")
    conn.commit()
    return ids


def score(conn, case: dict, ids: dict[str, int]) -> list[dict]:
    rows = []
    for s in case["shows"]:
        rows.append(_score_show(conn, ids[s["date"]], s, case["artist"]))
    for d in case.get("discover") or []:
        found = conn.execute(
            "SELECT DISTINCT c.show_id FROM claims c JOIN shows s ON s.id = c.show_id"
            " WHERE c.source = ? AND c.native_id LIKE 'found:%' AND s.date = ?", (gemini.SOURCE, d["date"])).fetchone()
        rows.append(_score_show(conn, found[0] if found else None, d, case["artist"], discovered=True))
    return rows


def _score_show(conn, show_id, s: dict, artist: str, discovered: bool = False) -> dict:
    row = {"date": s["date"], "venue": s["venue"] + (" (new)" if discovered else ""),
           "expected": s.get("openers") or [], "found": [], "missing": [], "extra": [], "check": None}
    if show_id is None:
        row["missing"] = row["expected"]
        return row
    claims = conn.execute(
        "SELECT a.name, c.role, c.extra FROM claims c JOIN artists a ON a.id = c.artist_id"
        " WHERE c.show_id = ? AND c.source = ?", (show_id, gemini.SOURCE)).fetchall()
    festival = conn.execute("SELECT is_festival FROM shows WHERE id = ?", (show_id,)).fetchone()[0]
    found = [c["name"] for c in claims if c["role"] == "opener"]
    row["found"] = found
    row["checks"] = sorted({json.loads(c["extra"] or "{}").get("check") for c in claims
                            if c["role"] == "opener"} - {None})
    want = {norm(o) for o in row["expected"]}
    got = {norm(o) for o in found}
    row["missing"] = [o for o in row["expected"] if norm(o) not in got]
    row["extra"] = [o for o in found if norm(o) not in want and norm(o) != norm(artist)]
    if s.get("festival") is not None:
        row["festival"] = (bool(festival), bool(s["festival"]))
    return row


def run(data_dir: Path, names=None, everything: bool = False, log=print) -> dict:
    work = Path(data_dir) / "eval"
    totals = {"cases": 0, "expected": 0, "found": 0, "extra": 0, "festival_ok": 0, "festival_cases": 0,
              "searches": 0, "contextdev": 0, "searxng": 0, "credits": 0, "gemini_calls": 0, "tokens": 0}
    client = gemini.client_from_env(work)
    mb = Path(data_dir) / musicbrainz.FILE
    for case in load_cases(names, everything):
        if work.exists():
            shutil.rmtree(work)
        conn = db.connect(work / "eval.sqlite")
        if mb.exists():
            db.attach_musicbrainz(conn, mb)      # names match as they do in the real ledger
        ids = setup(conn, case)
        for sid in ids.values():
            state.ensure(conn, gemini.SOURCE, f"show:{sid}", priority=1)
        conn.commit()
        log(f"== {case['name']}")
        stats = gemini.ask(conn, client, max_requests=12, max_credits=24, log=log)
        for k in ("searches", "contextdev", "searxng", "credits", "gemini_calls", "tokens"):
            totals[k] += stats.get(k) or 0
        totals["cases"] += 1
        for r in score(conn, case, ids):
            totals["expected"] += len(r["expected"])
            totals["found"] += len(r["expected"]) - len(r["missing"])
            totals["extra"] += len(r["extra"])
            flag = ""
            if "festival" in r:
                totals["festival_cases"] += 1
                totals["festival_ok"] += r["festival"][0] == r["festival"][1]
                flag = f" festival={'yes' if r['festival'][0] else 'no'} (expected {'yes' if r['festival'][1] else 'no'})"
            log(f"   {r['date']} {r['venue']}: found {r['found'] or '-'} {r.get('checks') or ''}"
                + (f" MISSING {r['missing']}" if r["missing"] else "")
                + (f" EXTRA {r['extra']}" if r["extra"] else "") + flag)
        log(f"   {stats.get('searches')} searches ({stats.get('contextdev')} context.dev,"
            f" {stats.get('searxng')} SearXNG), "
            f"{stats.get('gemini_calls')} Gemini calls" + (f", stopped: {stats['stopped']}" if stats.get("stopped") else ""))
        conn.close()
    return totals
