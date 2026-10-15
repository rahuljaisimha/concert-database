"""Live smoke test: a few real requests per source, against a throwaway database.

Never touches the real ledger and makes at most ~10 requests in total, so it is
safe to run after any change to a source. It reports PASS/FAIL per source.
"""

import os
import shutil
from pathlib import Path

from . import db, seed
from .http import Fetcher
from .sources import jsonld, pollstar, setlistfm

TEST_ARTIST = "Flat Worms"
TEST_VENUE, TEST_CITY = "Zebulon", "Los Angeles"
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
    results: dict[str, tuple[bool, str]] = {}

    log(f"seeding from {graph}")
    n = seed.seed_from_graph(conn, graph)
    results["seed"] = (n > 1000, f"{n:,} artists")
    # Only the test artist is due, so setlist.fm requests go to it.
    conn.execute("UPDATE crawl_state SET next_due = '9999-01-01'")
    seed.prioritize(conn, [TEST_ARTIST])
    conn.execute(
        "UPDATE crawl_state SET next_due = '2000-01-01' WHERE priority = ?", (seed.USER_PRIORITY,))
    conn.commit()

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
        ok &= passed
        log(f"{'PASS' if passed else 'FAIL'}  {name:<16} {detail}")
    log(f"\nraw responses saved under {work_dir / 'raw'} (useful as test fixtures)")
    conn.close()
    return ok


def _count(conn, source: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM claims WHERE source = ?", (source,)).fetchone()[0]
