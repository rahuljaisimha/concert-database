"""Command line: concertdb <command>. Run `concertdb -h` for the list."""

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

from . import backup, db, export, ledger, seed, smoke, state, status
from .http import Fetcher
from .normalize import norm
from .sources import jsonld, pollstar, setlistfm


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
    venue_id = ledger.get_or_create_venue(conn, args.venue, args.city, args.state, args.country)
    show_id = ledger.find_or_create_show(conn, args.date, venue_id)
    native = f"manual:{args.date}:{venue_id}"
    acts = [(args.headliner, "headliner")] + [(o, "opener") for o in args.opener or []]
    for pos, (name, role) in enumerate(acts):
        aid = ledger.get_or_create_artist(conn, name, None)
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


def cmd_crawl_setlistfm(args):
    conn = _conn(args)
    stats = setlistfm.crawl(conn, _setlistfm_client(args), max_requests=args.max_requests)
    print(stats)


def cmd_run(args):
    """One scheduled cycle: watched pages, setlist.fm, Pollstar, backup. Safe to run often."""
    conn = _conn(args)
    print("== capture (schema.org pages)")
    print(jsonld.capture(conn, Fetcher(_data_dir(args), min_interval=1.0)))
    if os.environ.get("SETLISTFM_API_KEY"):
        print("== setlist.fm")
        print(setlistfm.crawl(conn, _setlistfm_client(args), max_requests=args.setlistfm_requests))
    else:
        print("== setlist.fm skipped (no SETLISTFM_API_KEY)")
    # After setlist.fm, so venues it found today get their Pollstar lookup today.
    print("== Pollstar (upcoming shows)")
    print(pollstar.capture(conn, _pollstar_client(args), max_requests=args.pollstar_requests))
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


def cmd_restore(args):
    backup.restore(Path(args.db), _data_dir(args))


def cmd_smoke_test(args):
    ok = smoke.run(_data_dir(args) / "smoke", args.graph, args.page_url)
    sys.exit(0 if ok else 1)


def _lock(args):
    """One crawler at a time: a scheduled run that overlaps a long one exits quietly."""
    path = Path(args.db).with_suffix(".lock")
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
        print("another concertdb run is in progress; exiting")
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
    s.set_defaults(fn=cmd_seed, locked=True)

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
    s.set_defaults(fn=cmd_capture_run, locked=True)

    s = sub.add_parser("pollstar-add", help="watch a venue's upcoming Pollstar events")
    s.add_argument("venue")
    s.add_argument("--city")
    s.set_defaults(fn=cmd_pollstar_add, locked=True)

    s = sub.add_parser("pollstar", help="fetch due Pollstar venue lists and lookups")
    s.add_argument("--max-requests", type=int, default=pollstar.DAILY_LIMIT)
    s.set_defaults(fn=cmd_pollstar, locked=True)

    s = sub.add_parser("crawl-setlistfm", help="spend setlist.fm requests on due artists/venues")
    s.add_argument("--max-requests", type=int, default=setlistfm.DAILY_LIMIT)
    s.set_defaults(fn=cmd_crawl_setlistfm, locked=True)

    s = sub.add_parser("run", help="one scheduled cycle: watched pages, setlist.fm, Pollstar, backup")
    s.add_argument("--setlistfm-requests", type=int, default=setlistfm.DAILY_LIMIT)
    s.add_argument("--pollstar-requests", type=int, default=pollstar.DAILY_LIMIT)
    s.add_argument("--no-backup", action="store_true")
    s.set_defaults(fn=cmd_run, locked=True)

    s = sub.add_parser("status", help="coverage and progress")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("export", help="write opened_for edges for MusicSceneMap")
    s.add_argument("--out", default="data/export/opened_for.csv")
    s.add_argument("--min-conf", type=float, default=0.4)
    s.add_argument("--graph-only", action="store_true", help="only artists in the MusicSceneMap graph")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("smoke-test", help="a few live requests per source into a throwaway database")
    s.add_argument("--graph", default=seed.GRAPH_URL)
    s.add_argument("--page-url", help="also test one venue calendar / event page")
    s.set_defaults(fn=cmd_smoke_test)

    sub.add_parser("backup", help="snapshot (and upload to R2 if configured)").set_defaults(fn=cmd_backup)
    sub.add_parser("restore", help="restore the newest R2 snapshot").set_defaults(fn=cmd_restore)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    lock = _lock(args) if getattr(args, "locked", False) else None
    try:
        args.fn(args)
    finally:
        if lock:
            lock.close()


if __name__ == "__main__":
    main()
