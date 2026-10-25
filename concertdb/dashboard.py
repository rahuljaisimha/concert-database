"""data/dashboard.html: what the ledger holds (concerts, artists, lineups) and how
the scheduled runs are going (requests, credits, outcomes, backups).

collect() gathers everything into one dict; write() saves a snapshot of the
headline numbers (for trends) and renders a single self-contained HTML page
(no network needed to open it). Written after every run, gemini and pollstar
step, and by `concertdb dashboard`.
"""

import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import db, export, review, runs, state, status
from .sources import gemini, pollstar, setlistfm

STEPS = ["setlistfm", "gemini", "pollstar", "capture", "backup", "misses"]
USAGE_SOURCES = ["setlistfm", "gemini", "contextdev", "searxng", "firecrawl", "pollstar", "jsonld"]
USAGE_DAYS = 14
NEW_SHOW_DAYS = 30
COVERAGE_TREND_DAYS = 31            # completeness history sent to the page (7- and 30-day views)
YEARS_SHOWN = 30
BACKUP_STALE_HOURS = 26
CONTEXTDEV_START = 51_000           # the free credits context.dev gave us (for the balance chart)
# The process locks (cli._lock) a step runs under: Pollstar inside `run` or as its own process.
STEP_LOCKS = {"pollstar": ("pollstar", "crawl"), "gemini": ("gemini",), "setlistfm": ("crawl",),
              "capture": ("crawl",), "backup": ("crawl",), "misses": ("crawl",)}
PRIORITY_SHOWN = 15                 # prioritized artists listed (the next ones setlist.fm will crawl)
PRIORITY_DONE_SHOWN = 5             # and the ones it finished most recently
# The headline numbers kept in metric_snapshots, for the trend charts and deltas.
HEADLINE = ["concerts", "artists_with_concerts", "concerts_2plus", "opened_for_pairs",
            "venues_with_concerts", "upcoming", "coverage_pct", "coverage_recent_pct"]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def data_metrics(conn: sqlite3.Connection, today: date | None = None) -> dict:
    """The business side: concerts, artists, lineups, coverage."""
    today = (today or date.today()).isoformat()
    one = lambda q, *a: conn.execute(q, a).fetchone()[0]  # noqa: E731
    live = "s.status NOT IN ('cancelled', 'postponed')"
    m = {}
    m["concerts"] = one(f"SELECT COUNT(*) FROM shows s WHERE {live}")
    m["cancelled"] = one("SELECT COUNT(*) FROM shows WHERE status IN ('cancelled', 'postponed')")
    m["upcoming"] = one(f"SELECT COUNT(*) FROM shows s WHERE {live} AND s.date >= ?", today)
    m["past"] = m["concerts"] - m["upcoming"]
    m["festivals"] = one(f"SELECT COUNT(*) FROM shows s WHERE {live} AND s.is_festival = 1")

    sizes = dict(conn.execute(
        f"SELECT MIN(n, 4), COUNT(*) FROM (SELECT s.id, COUNT(b.artist_id) AS n FROM shows s"
        f" LEFT JOIN billings b ON b.show_id = s.id WHERE {live} GROUP BY s.id) GROUP BY MIN(n, 4)").fetchall())
    m["lineup_sizes"] = [{"label": lab, "concerts": sizes.get(k, 0)}
                         for k, lab in [(0, "0 acts"), (1, "1 act"), (2, "2 acts"), (3, "3 acts"), (4, "4+ acts")]]
    m["concerts_2plus"] = sum(sizes.get(k, 0) for k in (2, 3, 4))
    m["concerts_with_opener"] = one(
        f"SELECT COUNT(DISTINCT b.show_id) FROM billings b JOIN shows s ON s.id = b.show_id"
        f" WHERE b.role = 'opener' AND {live}")

    m["artists_with_concerts"] = one("SELECT COUNT(DISTINCT artist_id) FROM billings")
    m["artists_with_concerts_mb"] = one(
        "SELECT COUNT(DISTINCT b.artist_id) FROM billings b JOIN artists a ON a.id = b.artist_id"
        " WHERE a.mbid IS NOT NULL")
    m["headliners"] = one("SELECT COUNT(DISTINCT artist_id) FROM billings WHERE role = 'headliner'")
    m["openers"] = one("SELECT COUNT(DISTINCT artist_id) FROM billings WHERE role = 'opener'")
    m["artists_with_shared_bill"] = one(
        "SELECT COUNT(DISTINCT artist_id) FROM billings WHERE show_id IN"
        " (SELECT show_id FROM billings GROUP BY show_id HAVING COUNT(*) >= 2)")
    m["artists_known"] = one("SELECT COUNT(*) FROM artists WHERE mbid IS NOT NULL")
    m["artists_unmatched"] = one("SELECT COUNT(*) FROM artists WHERE mbid IS NULL")

    m["venues_with_concerts"] = one(f"SELECT COUNT(DISTINCT s.venue_id) FROM shows s WHERE {live}")
    m["cities"] = one(f"SELECT COUNT(DISTINCT v.city_norm || '|' || COALESCE(v.country, '')) FROM shows s"
                      f" JOIN venues v ON v.id = s.venue_id WHERE {live} AND v.city_norm != ''")
    m["countries"] = one(f"SELECT COUNT(DISTINCT v.country) FROM shows s JOIN venues v ON v.id = s.venue_id"
                         f" WHERE {live} AND v.country IS NOT NULL")
    m["top_countries"] = [{"label": c or "unknown", "concerts": n} for c, n in conn.execute(
        f"SELECT v.country, COUNT(*) AS n FROM shows s JOIN venues v ON v.id = s.venue_id WHERE {live}"
        f" GROUP BY v.country ORDER BY n DESC LIMIT 10")]

    # What MusicSceneMap gets: `export --musicbrainz-only` at the default confidence.
    m["opened_for_pairs"] = conn.execute(f"SELECT COUNT(*) FROM ({export.QUERY})",
                                         {"min_conf": 0.4, "known_only": 1}).fetchone()[0]

    first = today[:4]
    years = dict(conn.execute(f"SELECT substr(s.date, 1, 4), COUNT(*) FROM shows s WHERE {live}"
                              f" GROUP BY 1").fetchall())
    if years:
        hi = max(int(y) for y in years)
        lo = max(min(int(y) for y in years), hi - YEARS_SHOWN + 1)
        earlier = sum(n for y, n in years.items() if int(y) < lo)
        m["by_year"] = ([{"label": f"before {lo}", "concerts": earlier}] if earlier else []) + \
            [{"label": str(y), "concerts": years.get(str(y), 0), "future": str(y) > first}
             for y in range(lo, hi + 1)]
    else:
        m["by_year"] = []

    since = (_now() - timedelta(days=NEW_SHOW_DAYS - 1)).date().isoformat()
    added = dict(conn.execute("SELECT substr(created_at, 1, 10) AS d, COUNT(*) FROM shows"
                              " WHERE created_at >= ? GROUP BY d", (since,)).fetchall())
    m["added_by_day"] = [{"label": d, "concerts": added.get(d, 0)} for d in
                         ((date.fromisoformat(since) + timedelta(days=i)).isoformat() for i in range(NEW_SHOW_DAYS))]
    m["added_7d"] = sum(x["concerts"] for x in m["added_by_day"][-7:])

    m["by_source"] = [{"source": r["source"], "concerts": r["shows"], "opener_claims": r["openers"],
                       "artists": r["artists"]} for r in conn.execute(
        "SELECT source, COUNT(DISTINCT show_id) AS shows, SUM(role = 'opener') AS openers,"
        " COUNT(DISTINCT artist_id) AS artists FROM claims GROUP BY source ORDER BY shows DESC")]
    m["names_to_review"] = dict(conn.execute(
        "SELECT json_extract(extra, '$.name_check') AS k, COUNT(DISTINCT artist_id) FROM claims"
        " WHERE source = 'gemini' AND extra LIKE '%name_check%' AND k IN ('truncated', 'description')"
        " GROUP BY k").fetchall())
    m["coverage"] = coverage(conn, today)
    m["coverage_pct"] = m["coverage"]["overall"]
    m["coverage_recent"] = coverage(conn, today, since=years_ago(today, RECENT_YEARS))
    m["coverage_recent_pct"] = m["coverage_recent"]["overall"]
    return m


def _snapshots(conn: sqlite3.Connection) -> list[dict]:
    return [{"at": r["taken_at"], **json.loads(r["metrics"])}
            for r in conn.execute("SELECT * FROM metric_snapshots ORDER BY taken_at")]


def _before(snaps: list[dict], at: datetime) -> dict | None:
    """The latest snapshot taken at or before `at`."""
    iso = at.isoformat()
    older = [s for s in snaps if s["at"] <= iso]
    return older[-1] if older else None


def held_locks(conn: sqlite3.Connection, proc_locks: str = "/proc/locks") -> set[str] | None:
    """Which of the ledger's process locks are held right now, read from /proc/locks so
    nothing is locked (a probe that took the lock could make a starting run exit).
    None where that isn't available (not Linux, an in-memory ledger)."""
    main = next((r["file"] for r in conn.execute("PRAGMA database_list") if r["name"] == "main"), "")
    if not main or not os.path.exists(proc_locks):
        return None
    db_path = Path(main)
    inodes = {}
    for name in {n for names in STEP_LOCKS.values() for n in names}:
        p = db_path.with_name(f"{db_path.stem}.{name}.lock")
        if p.exists():
            inodes[p.stat().st_ino] = name
    held = set()
    for line in Path(proc_locks).read_text().splitlines():
        parts = line.split()
        if len(parts) > 5 and parts[1] == "FLOCK" and ":" in parts[5]:
            try:
                ino = int(parts[5].rsplit(":", 1)[1])
            except ValueError:
                continue
            if ino in inodes:
                held.add(inodes[ino])
    return held


MODEL_DAYS = 7


def gemini_models(conn: sqlite3.Connection, used: dict, days: list[str], recent: list[dict]) -> dict:
    """Gemini calls per day by model, and per model over the last MODEL_DAYS days: calls,
    tokens, and what its answers added (shows whose bill it confirmed, shows it found)."""
    configured = gemini.configured_models()
    seen = [s.split(":", 1)[1] for s in used if s.startswith(f"{gemini.SOURCE}:")]
    models = configured + sorted(m for m in set(seen) if m not in configured)
    per_day = {m: [used.get(gemini.model_usage(m), {}).get(d, 0) for d in days] for m in models}
    total = [used.get(gemini.SOURCE, {}).get(d, 0) for d in days]
    # Calls from before calls were counted per model (or by an eval with GEMINI_MODEL).
    earlier = [max(0, t - sum(per_day[m][i] for m in models)) for i, t in enumerate(total)]
    rows = [{"label": d, **{m: per_day[m][i] for m in models}, "_earlier": earlier[i]} for i, d in enumerate(days)]

    since = (_now() - timedelta(days=MODEL_DAYS)).isoformat()
    week_days = days[-MODEL_DAYS:]
    found = {r["m"]: dict(r) for r in conn.execute(
        "SELECT json_extract(extra, '$.model') AS m,"
        " COUNT(DISTINCT CASE WHEN json_extract(extra, '$.check') = 'specific' THEN show_id END) AS specific,"
        " COUNT(DISTINCT CASE WHEN json_extract(extra, '$.check') = 'page' THEN show_id END) AS page,"
        " COUNT(DISTINCT CASE WHEN json_extract(extra, '$.check') = 'discovered' THEN show_id END) AS discovered"
        " FROM claims WHERE source = ? AND seen_at >= ? AND role != 'headliner' GROUP BY m",
        (gemini.SOURCE, since))}
    tokens = {}
    for x in recent:
        if x["step"] == "gemini" and x["started_at"] >= since:
            for m, n in (x["stats"].get("tokens_by_model") or {}).items():
                tokens[m] = tokens.get(m, 0) + n
    table = []
    for m in models:
        calls = sum(per_day[m][days.index(d)] for d in week_days)
        f = found.get(m, {})
        table.append({"model": m, "calls": calls, "tokens": tokens.get(m), "tokens_per_call":
                      round(tokens[m] / calls) if tokens.get(m) and calls else None,
                      "specific": f.get("specific", 0), "page": f.get("page", 0),
                      "discovered": f.get("discovered", 0)})
    return {"models": models, "days": rows, "has_earlier": any(earlier), "week": table,
            "limit": gemini.DAILY_LIMIT}


RECENT_YEARS = 5                    # the dashboard's "last 5 years" completeness view


def years_ago(today: str, years: int) -> str:
    d = date.fromisoformat(today)
    try:
        return d.replace(year=d.year - years).isoformat()
    except ValueError:                  # 29 February
        return d.replace(year=d.year - years, day=28).isoformat()


def coverage(conn: sqlite3.Connection, today: str, since: str | None = None) -> dict:
    """How complete the concert data is, as parts with a numerator and denominator each.

    With `since` (an ISO date), only shows from then on count: bills settled and Gemini's
    checks are counted over those shows, and instead of setlists held against setlist.fm's
    all-time total (setlist.fm has no per-period count), "last 5 years fetched" counts the
    checked artists whose pages we've read back past `since`: their full history, nothing on
    setlist.fm, or (pages come newest first) the oldest date reached on their own pages
    (cursor `reached`; for artists crawled before that was recorded, the oldest setlist.fm
    show we hold for them, which only their own pages have provided so far).

    100% would be: every artist we crawl has every show setlist.fm knows of in the
    ledger, and every past show has its whole bill. "Our artists" are the artist
    units setlist.fm crawls (the graph's artists, plus prioritized ones). setlist.fm's
    own count per artist is the only "expected shows" figure there is, and it's only
    known for artists crawled at least once (setlistfm_totals), so `overall` is the
    product of the parts and an estimate."""
    one = lambda q, *a: conn.execute(q, a).fetchone()[0] or 0  # noqa: E731
    art = "c.source = 'setlistfm' AND c.key LIKE 'artist:%'"
    units = one(f"SELECT COUNT(*) FROM crawl_state c WHERE {art}")
    checked = one(f"SELECT COUNT(*) FROM crawl_state c WHERE {art} AND c.last_checked IS NOT NULL")
    full = one(f"SELECT COUNT(*) FROM crawl_state c WHERE {art} AND c.cursor LIKE '%backfill_done%'")
    # Setlists held for artists whose total is known, capped at that total (a setlist.fm
    # show can have been merged with another source's, or the total can lag).
    expected, held, known = conn.execute(   # one indexed lookup per artist (idx_claims_artist)
        "SELECT SUM(total), SUM(MIN(total, (SELECT COUNT(DISTINCT c.show_id) FROM claims c"
        "  WHERE c.artist_id = substr(t.key, 8) AND c.source = 'setlistfm'))), COUNT(*)"
        " FROM setlistfm_totals t WHERE t.key LIKE 'artist:%'").fetchone()
    lo = since or "0000-01-01"
    past = "s.date < ? AND s.date >= ? AND s.is_festival = 0 AND s.status NOT IN ('cancelled', 'postponed')"
    shows = one(f"SELECT COUNT(*) FROM shows s WHERE {past}", today, lo)
    settled = one(f"SELECT COUNT(*) FROM shows s WHERE {past} AND"
                  " (SELECT COUNT(*) FROM billings b WHERE b.show_id = s.id) >= 2", today, lo)
    gem = ("FROM crawl_state g JOIN shows s ON s.id = CAST(substr(g.key, 6) AS INTEGER)"
           " WHERE g.source = 'gemini' AND s.date >= ?")
    g_total = one(f"SELECT COUNT(*) {gem}", lo)
    g_checked = one(f"SELECT COUNT(*) {gem} AND g.last_checked IS NOT NULL", lo)
    g_out = dict(conn.execute(   # what came of it (the queue entry's cursor; see sources/gemini.py)
        f"SELECT COALESCE(json_extract(g.cursor, '$.outcome'), 'unrecorded'), COUNT(*) {gem}"
        " AND g.last_checked IS NOT NULL GROUP BY 1", (lo,)).fetchall())
    if since:
        recent_done = one(
            f"SELECT COUNT(*) FROM crawl_state c WHERE {art} AND c.last_checked IS NOT NULL AND ("
            "  c.cursor LIKE '%backfill_done%'"
            "  OR json_extract(c.cursor, '$.reached') < ?"
            "  OR (json_extract(c.cursor, '$.reached') IS NULL AND ("
            # `+cl.source`: look claims up by artist (idx_claims_artist), not by source (20 s -> <1 s)
            "      NOT EXISTS (SELECT 1 FROM claims cl WHERE cl.artist_id = substr(c.key, 8) AND +cl.source = 'setlistfm')"
            "      OR EXISTS (SELECT 1 FROM claims cl JOIN shows s ON s.id = cl.show_id"
            "                 WHERE cl.artist_id = substr(c.key, 8) AND +cl.source = 'setlistfm' AND s.date < ?))))",
            since, since)

    def part(key, label, num, den, num_label, den_label, note, breakdown=None):
        return {"key": key, "label": label, "num": num or 0, "den": den or 0, "num_label": num_label,
                "den_label": den_label, "note": note, "pct": round(100 * (num or 0) / den, 1) if den else None,
                "breakdown": breakdown or []}

    period = f"since {since}" if since else ""
    history = (part("history_held", "Their last 5 years fetched", recent_done, checked,
                    f"checked artists with every setlist.fm show {period} fetched", "artists checked on setlist.fm",
                    "setlist.fm gives no count per period, so this counts artists, not setlists.")
               if since else
               part("history_held", "Their setlists held", held, expected,
                    "setlists we hold", "setlists setlist.fm lists for them",
                    f"For the {known or 0:,} artists whose setlist.fm total we've seen."))
    parts = [
        part("artists_checked", "Artists checked on setlist.fm", checked, units,
             "artists setlist.fm has looked at", "artists we crawl",
             "Checked at least once (or found not to be on setlist.fm)."),
        history,
        part("bills_settled", "Bills settled", settled, shows,
             "past shows with 2+ acts" + (f" {period}" if since else ""),
             "past shows" + (f" {period}" if since else "") + " (not festivals, not cancelled)",
             "A show with one act listed may be missing its openers."),
        *([] if since else [part("artists_full", "Artists with full history", full, units,
             "artists with their whole setlist.fm history", "artists we crawl",
             "The newest 10 pages come first; older history waits for the deep pass.")]),
        part("gemini_checked", "Shows checked by Gemini", g_checked, g_total,
             "shows Gemini has looked at" + (f" {period}" if since else ""),
             "shows checked or waiting for Gemini" + (f" {period}" if since else ""),
             "Nothing found still counts as checked: those shows stay unknown and aren't asked again.",
             [[g_out[k], lab] for k, lab in (("specific", "bill confirmed"), ("page_only", "tour-wide only"),
                                            ("festival", "festival"), ("not_found", "nothing found"),
                                            ("unrecorded", "outcome not recorded")) if g_out.get(k)]),
    ]
    p = {x["key"]: x for x in parts}
    overall = None
    if all(p[k]["den"] for k in ("artists_checked", "history_held", "bills_settled")):
        overall = round(100 * (p["artists_checked"]["num"] / p["artists_checked"]["den"])
                        * (p["history_held"]["num"] / p["history_held"]["den"])
                        * (p["bills_settled"]["num"] / p["bills_settled"]["den"]), 2)
    return {"overall": overall, "parts": parts, "since": since}


def run_metrics(conn: sqlite3.Connection) -> dict:
    """The developer side: the steps' runs, requests per day, credits, queues."""
    r = {}
    recent = runs.recent(conn, limit=400)
    r["recent"] = recent[:40]
    r["last"] = {step: next((x for x in recent if x["step"] == step), None) for step in STEPS}
    held = held_locks(conn)
    for step, x in r["last"].items():         # only a step's latest run can still be going
        if x and not x["finished_at"]:
            x["running"] = None if held is None else any(n in held for n in STEP_LOCKS.get(step, ()))
    r["gemini_runs"] = [x for x in recent if x["step"] == "gemini" and x["finished_at"]][:20][::-1]
    r["setlistfm_runs"] = [x for x in recent if x["step"] == "setlistfm" and x["finished_at"]][:30][::-1]

    days = [(_now().date() - timedelta(days=USAGE_DAYS - 1 - i)).isoformat() for i in range(USAGE_DAYS)]
    used = {}
    for row in conn.execute("SELECT day, source, requests FROM usage WHERE day >= ?", (days[0],)):
        used.setdefault(row["source"], {})[row["day"]] = row["requests"]
    r["usage_days"] = days
    r["usage"] = {src: [used.get(src, {}).get(d, 0) for d in days] for src in USAGE_SOURCES}
    r["gemini_models"] = gemini_models(conn, used, days, recent)
    r["limits"] = {"setlistfm": 1400, "gemini": gemini.DAILY_LIMIT * len(gemini.configured_models())}

    bal = [{"at": x["finished_at"], "balance": x["stats"]["contextdev_balance"]}
           for x in recent if x["step"] == "gemini" and isinstance(x["stats"].get("contextdev_balance"), int)][::-1]
    week = sum(r["usage"]["contextdev"][-7:])
    per_day = week / 7
    r["contextdev"] = {"history": bal, "balance": bal[-1]["balance"] if bal else None,
                       "start": CONTEXTDEV_START, "per_day_7d": round(per_day, 1),
                       "days_left": round(bal[-1]["balance"] / per_day) if bal and per_day else None,
                       "cap_per_run": gemini.CONTEXTDEV_MAX_CREDITS}

    r["pollstar"] = {"interval": pollstar.current_interval(conn), "paused_until": pollstar.paused_until(conn),
                     "next_pause_hours": pollstar.pause_hours(conn),
                     "requests_today": state.requests_today(conn, "pollstar")}

    b = next((x for x in recent if x["step"] == "backup" and x["ok"]), None)
    age = (_now() - datetime.fromisoformat(b["finished_at"])).total_seconds() / 3600 if b else None
    r["backup"] = {"at": b["finished_at"] if b else None, "hours_ago": round(age, 1) if age is not None else None,
                   "bytes": b["stats"].get("bytes") if b else None,
                   "uploaded": b["stats"].get("uploaded") if b else None,
                   "stale": age is None or age > BACKUP_STALE_HOURS}
    s = status.summary(conn)
    r["queues"] = s["crawl"]
    r["setlistfm_deep_pending"] = s["setlistfm_deep_pending"]
    r["prioritized"] = prioritized(conn)
    return r


def prioritized(conn: sqlite3.Connection, shown: int = PRIORITY_SHOWN, done_shown: int = PRIORITY_DONE_SHOWN) -> dict:
    """Artists queued at the `prioritize` priority (by hand, or from MusicSceneMap's search
    misses): the next `shown` still to get their full setlist.fm history, in the order
    setlist.fm will take them (state.due: priority, then due time), and the ones finished last."""
    rows = conn.execute(
        "SELECT c.key, c.next_due, c.last_checked, c.cursor, a.id, a.name, a.disambiguation,"
        " (SELECT COUNT(DISTINCT b.show_id) FROM billings b WHERE b.artist_id = a.id) AS concerts"
        " FROM crawl_state c LEFT JOIN artists a ON a.id = substr(c.key, 8)"
        " WHERE c.source = ? AND c.key LIKE 'artist:%' AND c.priority >= ?"
        " ORDER BY c.priority DESC, c.next_due ASC",
        (setlistfm.SOURCE, setlistfm.NO_CAP_PRIORITY)).fetchall()

    def item(r) -> dict:
        cur = state.get_cursor(r)
        return {"mbid": r["key"][7:], "name": r["name"] or r["key"][7:], "disambiguation": r["disambiguation"],
                "concerts": r["concerts"] or 0, "last_checked": r["last_checked"], "next_due": r["next_due"],
                "next_page": cur.get("next_page"), "done": bool(cur.get("backfill_done"))}

    items = [item(r) for r in rows]
    waiting = [x for x in items if not x["done"]]
    done = sorted((x for x in items if x["done"]), key=lambda x: x["last_checked"] or "", reverse=True)
    return {"waiting": len(waiting), "done": len(done), "next": waiting[:shown], "recent": done[:done_shown]}


def alerts(d: dict, r: dict) -> list[dict]:
    """Things worth a look, worst first."""
    out = []
    if r["backup"]["stale"] and r["backup"]["at"]:
        out.append({"level": "critical", "text": f"No successful backup in the last {BACKUP_STALE_HOURS} hours"})
    elif r["backup"]["stale"]:
        out.append({"level": "warning", "text": "No backup recorded yet (the next scheduled run records one)"})
    elif r["backup"]["uploaded"] is False:
        out.append({"level": "warning", "text": "Last backup was local only (R2 not configured)"})
    for step, x in r["last"].items():
        if x and x["finished_at"] and not x["ok"]:
            out.append({"level": "serious", "text": f"Last {step} run failed: {x['stats'].get('error', '?')}"})
        elif x and not x["finished_at"] and (x.get("running") is False or (
                x.get("running") is None
                and _now() - datetime.fromisoformat(x["started_at"]) > timedelta(hours=12))):
            out.append({"level": "serious", "text": f"Last {step} run stopped without finishing (killed?)"})
    if r["pollstar"]["paused_until"]:
        out.append({"level": "warning", "text": "Pollstar paused until", "at": r["pollstar"]["paused_until"]})
    cd = r["contextdev"]
    if cd["days_left"] is not None and cd["days_left"] < 14:
        out.append({"level": "warning", "text": f"context.dev credits run out in about {cd['days_left']} days"})
    to_review = sum(d["names_to_review"].values())
    if to_review:
        out.append({"level": "info", "text": f"{to_review:,} flagged act names to review", "href": "#review"})
    return out


def collect(conn: sqlite3.Connection, today: date | None = None, snapshot: bool = True) -> dict:
    d = data_metrics(conn, today)
    now = _now()
    if snapshot:
        db.begin_write(conn)
        # Each completeness part's numerator and denominator too, so its trend can be drawn later.
        cov = {p["key"]: [p["num"], p["den"]] for p in d["coverage"]["parts"]}
        recent = {p["key"]: [p["num"], p["den"]] for p in d["coverage_recent"]["parts"]}
        conn.execute("INSERT OR REPLACE INTO metric_snapshots (taken_at, metrics) VALUES (?, ?)",
                     (now.isoformat(), json.dumps({**{k: d[k] for k in HEADLINE}, "coverage": cov,
                                                   "coverage_recent": recent})))
        conn.commit()
    snaps = _snapshots(conn)
    d["trend"] = [{k: v for k, v in s.items() if k not in ("coverage", "coverage_recent")} for s in snaps[-200:]]
    since = (now - timedelta(days=COVERAGE_TREND_DAYS)).isoformat()
    d["coverage_trend"] = [{"at": s["at"], "overall": s.get("coverage_pct"), "parts": s["coverage"],
                            "recent_overall": s.get("coverage_recent_pct"), "recent": s.get("coverage_recent")}
                           for s in snaps if s["at"] >= since and s.get("coverage")]
    d["deltas"] = {}
    for label, ago in (("day", timedelta(days=1)), ("week", timedelta(days=7))):
        base = _before(snaps, now - ago)
        d["deltas"][label] = {k: d[k] - base[k] for k in HEADLINE
                              if base.get(k) is not None and d.get(k) is not None} if base else None
    r = run_metrics(conn)
    return {"generated_at": now.isoformat(), "data": d, "runs": r, "alerts": alerts(d, r),
            "review": review.flagged(conn)}


def render(info: dict) -> str:
    blob = json.dumps(info, default=str).replace("</", "<\\/")
    return TEMPLATE.replace("/*DATA*/null", blob)


def write(conn: sqlite3.Connection, paths: list[Path]) -> list[Path]:
    html = render(collect(conn))
    out = []
    for p in paths:
        p = Path(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(html, encoding="utf-8")
        tmp.replace(p)
        out.append(p)
    return out


TEMPLATE_PATH = Path(__file__).with_name("dashboard_template.html")
TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")
