"""Coverage and progress summary."""

import sqlite3
from datetime import datetime, timezone

from . import state


def summary(conn: sqlite3.Connection) -> dict:
    one = lambda q, *a: conn.execute(q, a).fetchone()[0]  # noqa: E731
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    out = {
        "artists_in_graph": one("SELECT COUNT(*) FROM artists WHERE in_graph = 1"),
        "artists_unresolved": one("SELECT COUNT(*) FROM artists WHERE id LIKE 'name:%'"),
        "venues": one("SELECT COUNT(*) FROM venues"),
        "shows": one("SELECT COUNT(*) FROM shows"),
        "shows_with_opener": one(
            "SELECT COUNT(DISTINCT show_id) FROM billings WHERE role = 'opener'"),
        "single_act_shows": one(
            "SELECT COUNT(*) FROM (SELECT show_id FROM billings GROUP BY show_id HAVING COUNT(*) = 1)"),
        "groups_with_known_opener_show": one(
            "SELECT COUNT(DISTINCT b.artist_id) FROM billings b JOIN artists a ON a.id = b.artist_id"
            " WHERE a.type = 'group' AND b.show_id IN"
            " (SELECT show_id FROM billings WHERE role = 'opener')"),
        "groups_in_graph": one("SELECT COUNT(*) FROM artists WHERE in_graph = 1 AND type = 'group'"),
        "claims_by_source": dict(conn.execute(
            "SELECT source, COUNT(*) FROM claims GROUP BY source").fetchall()),
        "setlistfm_deep_pending": one(
            "SELECT COUNT(*) FROM crawl_state WHERE source = 'setlistfm'"
            " AND cursor LIKE '%\"deep\": true%' AND cursor NOT LIKE '%backfill_done%'"),
        "usd_this_month": round(state.usd_this_month(conn), 2),
        "crawl": {},
    }
    for r in conn.execute(
        "SELECT source,"
        " SUM(next_due <= ?) AS due,"
        " SUM(last_checked IS NOT NULL) AS checked,"
        " COUNT(*) AS total,"
        " SUM(failures > 0) AS failing"
        " FROM crawl_state GROUP BY source", (now,)
    ).fetchall():
        out["crawl"][r["source"]] = {
            "due": r["due"], "checked": r["checked"], "total": r["total"], "failing": r["failing"],
            "requests_today": state.requests_today(conn, r["source"]),
        }
    return out


def format_summary(s: dict) -> str:
    g = s["groups_in_graph"] or 1
    lines = [
        f"Artists in graph:     {s['artists_in_graph']:,}  (+{s['artists_unresolved']:,} seen in listings, not matched)",
        f"Venues:               {s['venues']:,}",
        f"Shows:                {s['shows']:,}",
        f"  with an opener:     {s['shows_with_opener']:,}",
        f"  single act listed:  {s['single_act_shows']:,}  (opener may be missing)",
        f"Groups with ≥1 show with a known opener: {s['groups_with_known_opener_show']:,}"
        f" / {s['groups_in_graph']:,} ({100 * s['groups_with_known_opener_show'] / g:.1f}%)",
        f"Claims by source:     {s['claims_by_source']}",
        f"Spend this month:     ${s['usd_this_month']:.2f}",
        "Crawl state:",
    ]
    for src, c in sorted(s["crawl"].items()):
        lines.append(
            f"  {src:<10} {c['checked']:,}/{c['total']:,} checked, {c['due']:,} due,"
            f" {c['failing']:,} failing, {c['requests_today']:,} requests today"
        )
        if src == "setlistfm" and s["setlistfm_deep_pending"]:
            lines.append(f"             {s['setlistfm_deep_pending']:,} with older history still to fetch (deep pass)")
    return "\n".join(lines)
