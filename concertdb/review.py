"""Reviewing act names the Gemini step flagged.

The Gemini step keeps every act name it reads but flags two kinds it isn't sure
of, in the claim's extra (see name_check() in sources/gemini.py):

  truncated    — cut short on the page ("The Blind Shak...") and not completable
  description  — reads like a description of acts, not a name
                 ("Josey Scott: The Original Voice Of Saliva")

`concertdb review` lists them, and settles each one:

  keep NAME           the name is right: drop the flag
  rename NAME NEW     store the act as NEW (matched to MusicBrainz like any name)
  mbid NAME MBID      it is this MusicBrainz artist
  delete NAME         not an act: remove its claims (a show left empty goes too)

NAME is the act's name as stored (or its artist id). A rename or MBID moves every
claim of the flagged artist to the right one and drops the old artist; the claim
keeps the name the page gave as extra.name_as_given.
"""

import json
import re
import sqlite3

from . import db, ledger
from .normalize import norm

FLAGS = ("truncated", "description")
_MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class ReviewError(Exception):
    pass


def flagged(conn: sqlite3.Connection) -> list[dict]:
    """Flagged act names, oldest first, each with the shows it was claimed for."""
    rows = conn.execute(
        "SELECT c.id AS claim_id, c.artist_id, a.name, json_extract(c.extra, '$.name_check') AS flag,"
        " c.extra, c.url, c.role, c.seen_at, s.id AS show_id, s.date, v.name AS venue, v.city, v.country"
        " FROM claims c JOIN artists a ON a.id = c.artist_id JOIN shows s ON s.id = c.show_id"
        " LEFT JOIN venues v ON v.id = s.venue_id"
        " WHERE c.source = 'gemini' AND json_extract(c.extra, '$.name_check') IN (?, ?)"
        " ORDER BY c.seen_at, c.id", FLAGS).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        item = out.setdefault(r["artist_id"], {"artist_id": r["artist_id"], "name": r["name"], "flag": r["flag"],
                                               "suggestion": suggest(r["name"], r["flag"]), "shows": []})
        headliners = [h[0] for h in conn.execute(
            "SELECT a.name FROM billings b JOIN artists a ON a.id = b.artist_id"
            " WHERE b.show_id = ? AND b.role = 'headliner' AND b.artist_id != ? ORDER BY b.position",
            (r["show_id"], r["artist_id"]))]
        item["shows"].append({"show_id": r["show_id"], "date": r["date"], "venue": r["venue"], "city": r["city"],
                              "country": r["country"], "role": r["role"], "headliners": headliners,
                              "url": r["url"], "quote": json.loads(r["extra"] or "{}").get("quote"),
                              "seen_at": r["seen_at"]})
    return list(out.values())


def suggest(name: str, flag: str | None) -> str | None:
    """A likely act name for a description: the part before a colon or a spaced dash."""
    if flag != "description":
        return None
    head = re.split(r"\s*:\s+|\s+[-–—]\s+", name, maxsplit=1)[0].strip()
    return head if head and head != name else None


def find(conn: sqlite3.Connection, ref: str) -> dict:
    """The flagged act a NAME (or artist id) given on the command line means."""
    items = flagged(conn)
    hits = [x for x in items if x["artist_id"] == ref] or [x for x in items if norm(x["name"]) == norm(ref)]
    if not hits:
        names = ", ".join(repr(x["name"]) for x in items) or "none"
        raise ReviewError(f"no flagged act named {ref!r} (flagged now: {names})")
    if len(hits) > 1:
        raise ReviewError(f"{ref!r} matches {len(hits)} flagged artists; use the artist id: "
                          + ", ".join(x["artist_id"] for x in hits))
    return hits[0]


def keep(conn: sqlite3.Connection, ref: str) -> dict:
    item = find(conn, ref)
    db.begin_write(conn)
    for (cid, extra) in conn.execute(
            "SELECT id, extra FROM claims WHERE artist_id = ? AND source = 'gemini'"
            " AND json_extract(extra, '$.name_check') IN (?, ?)", (item["artist_id"], *FLAGS)).fetchall():
        ex = json.loads(extra)
        ex["name_check"], ex["name_check_was"] = "kept", ex["name_check"]
        conn.execute("UPDATE claims SET extra = ? WHERE id = ?", (json.dumps(ex, sort_keys=True), cid))
    conn.commit()
    return {"action": "kept", "name": item["name"], "shows": len(item["shows"])}


def rename(conn: sqlite3.Connection, ref: str, new_name: str | None = None, mbid: str | None = None) -> dict:
    """Move the flagged artist's claims to NEW (or to the MusicBrainz artist MBID)."""
    if mbid is not None:
        mbid = mbid.strip().lower()
        if not _MBID.match(mbid):
            raise ReviewError(f"{mbid!r} is not a MusicBrainz ID (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)")
    if mbid is None and not (new_name or "").strip():
        raise ReviewError("give the new name")
    item = find(conn, ref)
    old = item["artist_id"]
    db.begin_write(conn)
    target = ledger.get_or_create_artist(conn, (new_name or item["name"]).strip(), mbid)
    if target == old:
        conn.rollback()
        raise ReviewError(f"{new_name!r} is the same artist as {item['name']!r}; use `keep` to drop the flag")
    shows = set()
    for c in conn.execute("SELECT id, show_id, source, native_id, extra FROM claims WHERE artist_id = ?",
                          (old,)).fetchall():
        shows.add(c["show_id"])
        if conn.execute("SELECT 1 FROM claims WHERE source = ? AND native_id = ? AND artist_id = ?",
                        (c["source"], c["native_id"], target)).fetchone():
            conn.execute("DELETE FROM claims WHERE id = ?", (c["id"],))   # the record already names the right act
            continue
        ex = json.loads(c["extra"] or "{}")
        if ex.get("name_check") in FLAGS:
            ex["name_check_was"] = ex["name_check"]
            ex["name_check"] = "renamed"
        ex.setdefault("name_as_given", item["name"])
        conn.execute("UPDATE claims SET artist_id = ?, extra = ? WHERE id = ?",
                     (target, json.dumps(ex, sort_keys=True), c["id"]))
    for s in shows:
        ledger.resolve_show(conn, s)
    _drop_artist_if_unused(conn, old)
    conn.commit()
    name = conn.execute("SELECT name FROM artists WHERE id = ?", (target,)).fetchone()[0]
    return {"action": "renamed", "name": item["name"], "to": name, "artist_id": target, "shows": len(shows)}


def delete(conn: sqlite3.Connection, ref: str) -> dict:
    """Not an act: remove every claim of the flagged artist, and shows left with none."""
    item = find(conn, ref)
    old = item["artist_id"]
    db.begin_write(conn)
    shows = {r[0] for r in conn.execute("SELECT show_id FROM claims WHERE artist_id = ?", (old,))}
    conn.execute("DELETE FROM claims WHERE artist_id = ?", (old,))
    dropped = 0
    for s in shows:
        if ledger.drop_show_if_empty(conn, s):
            dropped += 1
        else:
            ledger.resolve_show(conn, s)
    _drop_artist_if_unused(conn, old)
    conn.commit()
    return {"action": "deleted", "name": item["name"], "shows": len(shows), "shows_dropped": dropped}


def _drop_artist_if_unused(conn: sqlite3.Connection, artist_id: str) -> None:
    """A name-only artist nothing points at any more. MusicBrainz artists stay (they're seeds too)."""
    if artist_id.startswith("name:") and not conn.execute(
            "SELECT 1 FROM claims WHERE artist_id = ? UNION ALL SELECT 1 FROM billings WHERE artist_id = ? LIMIT 1",
            (artist_id, artist_id)).fetchone():
        conn.execute("DELETE FROM artists WHERE id = ?", (artist_id,))


def format_list(items: list[dict]) -> str:
    if not items:
        return "No flagged act names to review."
    lines = [f"{len(items)} flagged act name(s) to review:", ""]
    for x in items:
        lines.append(f"{x['name']!r}  ({x['flag']})")
        for s in x["shows"]:
            where = ", ".join(p for p in (s["venue"], s["city"], s["country"]) if p)
            bill = f" for {', '.join(s['headliners'])}" if s["headliners"] and s["role"] == "opener" else ""
            lines.append(f"    {s['date']} {where}: {s['role']}{bill}")
            if s["url"]:
                lines.append(f"      {s['url']}")
            if s["quote"]:
                lines.append(f"      \"{s['quote'][:160]}\"")
        q = shell_quote(x["name"])
        lines.append(f"    deploy/review.sh keep {q}")
        lines.append(f"    deploy/review.sh rename {q} {shell_quote(x['suggestion'] or 'NEW NAME')}")
        lines.append(f"    deploy/review.sh mbid {q} MBID")
        lines.append(f"    deploy/review.sh delete {q}")
        lines.append("")
    return "\n".join(lines).rstrip()


def shell_quote(s: str) -> str:
    """Quoted for bash, readable: double quotes unless the name needs single ones."""
    if not re.search(r'["$`\\!]', s):
        return f'"{s}"'
    return "'" + s.replace("'", "'\\''") + "'"
