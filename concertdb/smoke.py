"""Live smoke test: a few real requests per source, against a throwaway database.

Never touches the real ledger and makes at most ~10 requests in total, so it is
safe to run after any change to a source. It reports PASS/FAIL per source.
"""

import json
import os
import shutil
from pathlib import Path

from . import db, ledger, seed, state
from .http import Fetcher
from .normalize import norm
from .sources import gemini, jsonld, pollstar, setlistfm

TEST_ARTIST = "Flat Worms"
TEST_VENUE, TEST_CITY = "Zebulon", "Los Angeles"
# A past show whose opener is known: Gemini should find Flat Worms.
GEMINI_DATE, GEMINI_HEADLINER = "2026-09-17", "Iguana Death Cult"
SETLISTFM_REQUESTS = 3
POLLSTAR_REQUESTS = 3


def run(work_dir: Path, graph: str, page_url: str | None = None, log=print) -> bool:
    db_path = work_dir / "smoke.sqlite"
    for p in (db_path, work_dir / "raw"):
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    conn = db.connect(db_path)
    results: dict[str, tuple[bool | None, str]] = {}   # None = skipped

    log(f"seeding from {graph}")
    n = seed.seed_from_graph(conn, graph)
    results["seed"] = (n > 1000, f"{n:,} artists")
    # Only the test artist is due, so setlist.fm requests go to it.
    conn.execute("UPDATE crawl_state SET next_due = '9999-01-01'")
    seed.prioritize(conn, [TEST_ARTIST])
    conn.execute(
        "UPDATE crawl_state SET next_due = '2000-01-01' WHERE priority = ?", (seed.USER_PRIORITY,))
    conn.commit()

    # Before setlist.fm, so the show has only the headliner when Gemini is asked.
    gkey, fkey = os.environ.get("GEMINI_API_KEY"), os.environ.get("FIRECRAWL_API_KEY")
    results["Gemini"] = (_gemini(conn, work_dir, gkey, fkey, log) if gkey and fkey
                         else (None, "GEMINI_API_KEY or FIRECRAWL_API_KEY not set"))

    key = os.environ.get("SETLISTFM_API_KEY")
    if key:
        client = setlistfm.Client(Fetcher(work_dir, min_interval=setlistfm.MIN_INTERVAL), key)
        stats = setlistfm.crawl(conn, client, max_requests=SETLISTFM_REQUESTS, log=log)
        claims = _count(conn, "setlistfm")
        results["setlist.fm"] = (claims > 0, f"{stats['requests']} requests, {claims} setlists stored")
    else:
        results["setlist.fm"] = (False, "SETLISTFM_API_KEY not set")

    conn.execute("UPDATE crawl_state SET next_due = '9999-01-01' WHERE source = 'setlistfm'")
    pclient = pollstar.Client(Fetcher(work_dir, min_interval=pollstar.POLL_INTERVAL))
    v = pollstar.add_venue(conn, pclient, TEST_VENUE, TEST_CITY)
    if v:
        stats = pollstar.capture(conn, pclient, max_requests=POLLSTAR_REQUESTS - 1, log=log)
        claims = _count(conn, "pollstar")
        results["Pollstar"] = (claims > 0, f"venue {v['id']}, {stats['events']} upcoming events stored")
    else:
        results["Pollstar"] = (False, f"no match for {TEST_VENUE}, {TEST_CITY} (blocked or format changed?)")

    if page_url:
        jsonld.add_url(conn, page_url)
        stats = jsonld.capture(conn, Fetcher(work_dir), limit=1, log=log)
        results["schema.org page"] = (stats["events"] > 0, f"{stats['events']} events from {page_url}")

    conn.commit()
    log("")
    ok = True
    for name, (passed, detail) in results.items():
        ok &= passed is not False
        log(f"{'SKIP' if passed is None else 'PASS' if passed else 'FAIL'}  {name:<16} {detail}")
    log(f"\nraw responses saved under {work_dir / 'raw'} (useful as test fixtures)")
    conn.close()
    return ok


def _count(conn, source: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM claims WHERE source = ?", (source,)).fetchone()[0]


def _gemini(conn, work_dir: Path, key: str, search_key: str, log) -> tuple[bool, str]:
    """One Firecrawl search (2 credits), its pages, and one Gemini call, for a show with a known opener."""
    venue = ledger.get_or_create_venue(conn, TEST_VENUE, TEST_CITY)
    show = ledger.find_or_create_show(conn, GEMINI_DATE, venue)
    ledger.add_claim(conn, show, ledger.get_or_create_artist(conn, GEMINI_HEADLINER), "manual", "smoke",
                     role="headliner", confidence=1.0)
    state.ensure(conn, gemini.SOURCE, f"show:{show}", priority=10**9)
    conn.commit()
    client = gemini.Client(Fetcher(work_dir, min_interval=gemini.MIN_INTERVAL),
                           Fetcher(work_dir, min_interval=gemini.SEARCH_INTERVAL),
                           Fetcher(work_dir, min_interval=gemini.PAGE_INTERVAL),
                           key, search_key, model=os.environ.get("GEMINI_MODEL") or None)
    try:
        stats = gemini.ask(conn, client, max_requests=1, max_credits=gemini.SEARCH_CREDITS, log=log)
    except Exception as e:  # bad key, ...
        return False, str(e)[:200]
    row = conn.execute(
        "SELECT c.extra FROM claims c JOIN artists a ON a.id = c.artist_id"
        " WHERE c.source = ? AND c.show_id = ? AND a.name_norm = ? AND c.role = 'opener'",
        (gemini.SOURCE, show, norm(TEST_ARTIST))).fetchone()
    if row:
        check = json.loads(row["extra"])["check"]
        return True, (f"{stats['model']}: {TEST_ARTIST} found ({check}), {stats['searches']} search,"
                      f" {stats['pages']} pages, {stats['tokens']:,} tokens")
    return False, f"{stats.get('model')}: {TEST_ARTIST} not found {stats}"
