"""Command line: concertdb <command>. Run `concertdb -h` for the list."""

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

from . import backup, db, export, ledger, repair, seed, smoke, state, status
from .http import Fetcher
from .normalize import norm
from .sources import gemini, jsonld, pollstar, setlistfm

# Pollstar requests spent inside `run` when no separate Pollstar process is
# running (it keeps the daily run short; `concertdb pollstar` does the bulk).
RUN_POLLSTAR_REQUESTS = 300


def _conn(args):
    return db.connect(args.db)


def _data_dir(args) -> Path:
    return Path(args.db).parent


def cmd_init(args):
    _conn(args)
    print(f"ledger ready at {args.db}")


def cmd_seed(args):
    n = seed.seed_from_graph(_conn(args), args.graph)
    print(f"seeded {n:,} artists from {args.graph}")


def cmd_prioritize(args):
    names = list(args.artists)
    if args.file:
        names += [l.strip() for l in open(args.file) if l.strip() and not l.startswith("#")]
    missing = seed.prioritize(_conn(args), names)
    print(f"prioritized {len(names) - len(missing)} artist(s)")
    for m in missing:
        print(f"  not found (no MusicBrainz ID in the ledger): {m}")


def cmd_add_show(args):
    """Manual entry: the most trustworthy source there is."""
    conn = _conn(args)
    date.fromisoformat(args.date)  # validate
    db.begin_write(conn)
    venue_id = ledger.get_or_create_venue(conn, args.venue, args.city, args.state, args.country)
    native = f"manual:{args.date}:{venue_id}"
    acts = [(args.headliner, "headliner")] + [(o, "opener") for o in args.opener or []]
    aids = [ledger.get_or_create_artist(conn, name, None) for name, _ in acts]
    show_id = ledger.find_or_create_show(conn, args.date, venue_id, aids, source="manual", native_id=native)
    for pos, (aid, (_, role)) in enumerate(zip(aids, acts)):
        ledger.add_claim(conn, show_id, aid, "manual", native, role=role, confidence=1.0,
                         position=pos, url=args.url, extra={"note": args.note} if args.note else None)
    if args.status:
        ledger.set_show_status(conn, show_id, args.status)
    conn.commit()
    _print_show(conn, show_id)


def _print_show(conn, show_id):
    s = conn.execute(
        "SELECT s.date, s.status, v.name AS venue, v.city FROM shows s JOIN venues v ON v.id = s.venue_id"
        " WHERE s.id = ?", (show_id,)).fetchone()
    print(f"{s['date']}  {s['venue']}, {s['city'] or '?'}  [{s['status']}]")
    for b in conn.execute(
        "SELECT a.name, a.in_graph, b.role, b.confidence, b.n_sources FROM billings b"
        " JOIN artists a ON a.id = b.artist_id WHERE b.show_id = ?"
        " ORDER BY b.role = 'opener', b.position", (show_id,)):
        mark = "" if b["in_graph"] else "  (not in graph)"
        print(f"   {b['role']:<9} {b['name']}  conf {b['confidence']:.2f}, {b['n_sources']} source(s){mark}")


def cmd_shows(args):
    conn = _conn(args)
    rows = conn.execute(
        "SELECT DISTINCT s.id, s.date FROM shows s JOIN billings b ON b.show_id = s.id"
        " JOIN artists a ON a.id = b.artist_id WHERE a.name_norm = ? OR a.id = ?"
        " ORDER BY s.date DESC LIMIT ?",
        (norm(args.artist), args.artist, args.limit),
    ).fetchall()
    if not rows:
        print("no shows found")
    for r in rows:
        _print_show(conn, r["id"])


def cmd_capture_add(args):
    conn = _conn(args)
    for url in args.urls:
        jsonld.add_url(conn, url)
    conn.commit()
    print(f"watching {len(args.urls)} page(s)")


def cmd_capture_run(args):
    conn = _conn(args)
    stats = jsonld.capture(conn, Fetcher(_data_dir(args), min_interval=1.0), limit=args.limit)
    print(stats)


def _setlistfm_client(args):
    key = os.environ.get("SETLISTFM_API_KEY")
    if not key:
        sys.exit("set SETLISTFM_API_KEY (get one at https://www.setlist.fm/settings/api)")
    return setlistfm.Client(Fetcher(_data_dir(args), min_interval=setlistfm.MIN_INTERVAL), key)


def _pollstar_client(args):
    return pollstar.Client(Fetcher(_data_dir(args), min_interval=pollstar.POLL_INTERVAL))


def cmd_pollstar_add(args):
    conn = _conn(args)
    v = pollstar.add_venue(conn, _pollstar_client(args), args.venue, args.city)
    conn.commit()
    if v:
        print(f"watching Pollstar venue {v['id']}: {v['name']}, {v['city']} {v['state'] or ''}")
    else:
        print("no confident match on Pollstar (try the venue's exact name)")


def cmd_pollstar(args):
    conn = _conn(args)
    print(pollstar.capture(conn, _pollstar_client(args), max_requests=args.max_requests))


def cmd_pollstar_rematch(args):
    print(pollstar.rematch_archived(_conn(args), _data_dir(args)))


def cmd_crawl_setlistfm(args):
    conn = _conn(args)
    stats = setlistfm.crawl(conn, _setlistfm_client(args), max_requests=args.max_requests)
    print(stats)


def _gemini_client(args):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("set GEMINI_API_KEY (get one at https://aistudio.google.com/apikey)")
    search_key = os.environ.get("FIRECRAWL_API_KEY")
    if not search_key:
        sys.exit("set FIRECRAWL_API_KEY (https://www.firecrawl.dev/app/api-keys):"
                 " Gemini reads the pages a Firecrawl search finds")
    d = _data_dir(args)
    return gemini.Client(Fetcher(d, min_interval=gemini.MIN_INTERVAL),
                         Fetcher(d, min_interval=gemini.SEARCH_INTERVAL),
                         Fetcher(d, min_interval=gemini.PAGE_INTERVAL),
                         key, search_key, model=args.model or os.environ.get("GEMINI_MODEL") or None)


def _monthly_credits() -> int | None:
    v = os.environ.get("FIRECRAWL_MONTHLY_CREDITS")
    return int(v) if v else gemini.MONTHLY_CREDITS


def cmd_gemini(args):
    conn = _conn(args)
    print(gemini.ask(conn, _gemini_client(args), max_requests=args.max_requests,
                     max_credits=args.max_credits, monthly_credits=_monthly_credits(),
                     max_acts=args.max_acts))


def cmd_gemini_recheck(args):
    print(gemini.recheck(_conn(args), dry_run=args.dry_run))


def cmd_run(args):
    """One scheduled cycle: watched pages, setlist.fm, Pollstar, backup. Safe to run often.

    Pollstar is skipped while a separate `concertdb pollstar` process is running
    (it holds the Pollstar lock), so the two never crawl Pollstar at once.
    """
    conn = _conn(args)
    print("== capture (schema.org pages)")
    print(jsonld.capture(conn, Fetcher(_data_dir(args), min_interval=1.0)))
    if os.environ.get("SETLISTFM_API_KEY"):
        print("== setlist.fm")
        print(setlistfm.crawl(conn, _setlistfm_client(args), max_requests=args.setlistfm_requests))
    else:
        print("== setlist.fm skipped (no SETLISTFM_API_KEY)")
    # After setlist.fm, so venues it found today get their Pollstar lookup today.
    plock = None if args.pollstar_requests <= 0 else _lock(args, "pollstar", exit_if_busy=False)
    if plock:
        print("== Pollstar (upcoming shows)")
        try:
            print(pollstar.capture(conn, _pollstar_client(args), max_requests=args.pollstar_requests))
        finally:
            plock.close()
    else:
        print("== Pollstar skipped (" + ("--pollstar-requests 0" if args.pollstar_requests <= 0
                                         else "the separate Pollstar process is running") + ")")
    if not args.no_backup:
        cmd_backup(args, conn)


def cmd_status(args):
    s = status.summary(_conn(args))
    print(json.dumps(s, indent=2) if args.json else status.format_summary(s))


def cmd_export(args):
    rows = export.opened_for(_conn(args), min_conf=args.min_conf, graph_only=args.graph_only)
    export.write_csv(rows, args.out)
    print(f"wrote {len(rows):,} opened_for edges to {args.out}")


def cmd_backup(args, conn=None):
    conn = conn or _conn(args)
    snap = backup.snapshot(conn, _data_dir(args))
    print(f"snapshot {snap}")
    if backup.r2_configured():
        backup.upload(snap, _data_dir(args))
    else:
        print("R2 not configured (R2_* env vars); local snapshot only")
    # After the upload, so the newest days are in R2 before anything is removed here.
    backup.prune_raw(_data_dir(args), backup.raw_keep_days())


def cmd_restore(args):
    backup.restore(Path(args.db), _data_dir(args))


def cmd_repair_shows(args):
    """Split shows the old ±1-day rule merged (see concertdb/repair.py)."""
    plock = _lock(args, "pollstar")   # the Pollstar process writes shows too: wait for it to stop
    try:
        stats = repair.repair_shows(_conn(args), _data_dir(args), dry_run=args.dry_run)
    finally:
        plock.close()
    print(stats)


def cmd_smoke_test(args):
    ok = smoke.run(_data_dir(args) / "smoke", args.graph, args.page_url)
    sys.exit(0 if ok else 1)


def _lock(args, name: str, exit_if_busy: bool = True):
    """One crawler per lock name at a time ("crawl": run, setlist.fm, pages, seed;
    "pollstar": the Pollstar process). A scheduled run that overlaps a long one
    exits quietly; with exit_if_busy=False, returns None instead."""
    path = Path(args.db).with_name(f"{Path(args.db).stem}.{name}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        if not exit_if_busy:
            return None
        print(f"another concertdb {name} process is in progress; exiting")
        sys.exit(0)
    return f


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="concertdb", description=__doc__)
    p.add_argument("--db", default=os.environ.get("CONCERTDB_PATH", "data/ledger.sqlite"))
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the ledger").set_defaults(fn=cmd_init)

    s = sub.add_parser("seed", help="import artists from MusicSceneMap's graph.db(.gz)")
    s.add_argument("--graph", default=seed.GRAPH_URL,
                   help="path or URL to graph.db(.gz); default: MusicSceneMap's on GitHub")
    s.set_defaults(fn=cmd_seed, locked="crawl")

    s = sub.add_parser("prioritize", help="crawl these artists first (names or MBIDs)")
    s.add_argument("artists", nargs="*")
    s.add_argument("--file", help="text file, one artist per line")
    s.set_defaults(fn=cmd_prioritize)

    s = sub.add_parser("add-show", help="enter a show by hand")
    s.add_argument("--date", required=True, help="YYYY-MM-DD")
    s.add_argument("--venue", required=True)
    s.add_argument("--city", required=True)
    s.add_argument("--state")
    s.add_argument("--country")
    s.add_argument("--headliner", required=True)
    s.add_argument("--opener", action="append", help="repeat for several openers, in order")
    s.add_argument("--status", choices=["played", "cancelled", "postponed", "scheduled"])
    s.add_argument("--url")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_add_show)

    s = sub.add_parser("shows", help="list stored shows for an artist")
    s.add_argument("artist", help="name or MusicBrainz ID")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_shows)

    s = sub.add_parser("capture-add", help="watch venue calendar / event pages (schema.org data)")
    s.add_argument("urls", nargs="+")
    s.set_defaults(fn=cmd_capture_add)

    s = sub.add_parser("capture", help="recheck due watched pages")
    s.add_argument("--limit", type=int, default=200)
    s.set_defaults(fn=cmd_capture_run, locked="crawl")

    s = sub.add_parser("pollstar-add", help="watch a venue's upcoming Pollstar events")
    s.add_argument("venue")
    s.add_argument("--city")
    s.set_defaults(fn=cmd_pollstar_add, locked="pollstar")

    s = sub.add_parser("pollstar", help="fetch due Pollstar venue lists and lookups (long-running;"
                       " deploy/run-pollstar.sh runs it daily next to `run`)")
    s.add_argument("--max-requests", type=int, default=pollstar.DAILY_LIMIT)
    s.set_defaults(fn=cmd_pollstar, locked="pollstar")

    s = sub.add_parser("pollstar-rematch", help="retry Pollstar venue lookups that found no match,"
                       " from archived answers (no requests)")
    s.set_defaults(fn=cmd_pollstar_rematch, locked="pollstar")

    s = sub.add_parser("crawl-setlistfm", help="spend setlist.fm requests on due artists/venues")
    s.add_argument("--max-requests", type=int, default=setlistfm.DAILY_LIMIT)
    s.set_defaults(fn=cmd_crawl_setlistfm, locked="crawl")

    s = sub.add_parser("gemini", help="find openers of shows that lack one: Firecrawl search,"
                       " fetched pages, Gemini reads them (a tour at a time)")
    s.add_argument("--max-requests", type=int, default=gemini.DAILY_LIMIT, help="Gemini calls this run")
    s.add_argument("--max-credits", type=int, default=None,
                   help="Firecrawl credits this run (default: no cap; the run stops when Firecrawl"
                        " is out of credits, or at $FIRECRAWL_MONTHLY_CREDITS a calendar month if set)")
    s.add_argument("--model", help=f"default: $GEMINI_MODEL, else {gemini.MODEL}")
    s.add_argument("--max-acts", type=int, default=int(os.environ.get("GEMINI_MAX_ACTS") or gemini.MAX_ACTS),
                   help="only ask about shows with at most this many acts listed"
                        f" (default: $GEMINI_MAX_ACTS, else {gemini.MAX_ACTS})")
    s.set_defaults(fn=cmd_gemini, locked="gemini")

    s = sub.add_parser("gemini-recheck", help="re-test stored Gemini claims against the current rules"
                       " (festival lineups, quotes that don't name the show, undated new shows) and delete"
                       " what fails (no requests)")
    s.add_argument("--dry-run", action="store_true", help="only list what would be deleted")
    s.set_defaults(fn=cmd_gemini_recheck, locked="gemini")

    s = sub.add_parser("run", help="one scheduled cycle: watched pages, setlist.fm, Pollstar, backup")
    s.add_argument("--setlistfm-requests", type=int, default=setlistfm.DAILY_LIMIT)
    s.add_argument("--pollstar-requests", type=int, default=RUN_POLLSTAR_REQUESTS,
                   help="Pollstar budget inside `run` (0 = none); the separate `pollstar`"
                        " process has its own, larger budget")
    s.add_argument("--no-backup", action="store_true")
    s.set_defaults(fn=cmd_run, locked="crawl")

    s = sub.add_parser("status", help="coverage and progress")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("export", help="write opened_for edges for MusicSceneMap")
    s.add_argument("--out", default="data/export/opened_for.csv")
    s.add_argument("--min-conf", type=float, default=0.4)
    s.add_argument("--graph-only", action="store_true", help="only artists in the MusicSceneMap graph")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("repair-shows", help="split shows that the old ±1-day rule merged wrongly")
    s.add_argument("--dry-run", action="store_true", help="report what would change, save nothing")
    s.set_defaults(fn=cmd_repair_shows, locked="crawl")

    s = sub.add_parser("smoke-test", help="a few live requests per source into a throwaway database")
    s.add_argument("--graph", default=seed.GRAPH_URL)
    s.add_argument("--page-url", help="also test one venue calendar / event page")
    s.set_defaults(fn=cmd_smoke_test)

    sub.add_parser("backup", help="snapshot (and upload to R2 if configured)").set_defaults(fn=cmd_backup)
    sub.add_parser("restore", help="restore the newest R2 snapshot").set_defaults(fn=cmd_restore)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    lock = _lock(args, args.locked) if getattr(args, "locked", None) else None
    try:
        args.fn(args)
    finally:
        if lock:
            lock.close()


if __name__ == "__main__":
    main()
