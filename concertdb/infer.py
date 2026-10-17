"""Work out who headlined and who opened from per-artist setlists.

setlist.fm has one setlist per artist per show and no "opener" field. When
several artists have setlists for the same venue and night, these signals
decide the bill, strongest first:

1. The info note names the headliner ("Opening for X", "Support for X").
2. A tour name that contains another act's name (the opener's setlist is
   often filed under the headliner's tour).
3. Exactly one act has a tour name.
4. One act played clearly more songs than everyone else.

Pure functions (no database), so they are easy to test.
"""

import re
from dataclasses import dataclass

from .normalize import norm

_SUPPORT_RE = re.compile(
    r"\b(?:opening|opened|opener|support(?:ing)?|supported|warm(?:ing)?[ -]?up)"
    r"(?:\s+act)?\s+(?:for|to)\s+(?P<name>[^.,;()\n]+)",
    re.IGNORECASE,
)


@dataclass
class SetlistFacts:
    artist_id: str
    artist_name: str
    song_count: int = 0
    tour: str | None = None
    info: str | None = None


@dataclass
class Role:
    role: str          # headliner | opener | unknown
    confidence: float
    reason: str


def _supported_name(info: str | None) -> str | None:
    if not info:
        return None
    m = _SUPPORT_RE.search(info)
    return norm(m.group("name")) if m else None


def infer_bill(sets: list[SetlistFacts], festival_min_acts: int = 8) -> dict[str, Role]:
    """Assign a role to every artist on one night's bill."""
    roles = {s.artist_id: Role("unknown", 0.0, "single act") for s in sets}
    if len(sets) < 2:
        return roles
    if len(sets) >= festival_min_acts:
        return {s.artist_id: Role("unknown", 0.0, "festival") for s in sets}

    names = {s.artist_id: norm(s.artist_name) for s in sets}

    def headliner_is(aid: str, conf: float, reason: str) -> dict[str, Role]:
        return {
            s.artist_id: Role("headliner", conf, reason) if s.artist_id == aid
            else Role("opener", conf, reason)
            for s in sets
        }

    # 1. "Opening for X" in an info note.
    for s in sets:
        target = _supported_name(s.info)
        if not target:
            continue
        for other in sets:
            if other.artist_id != s.artist_id and names[other.artist_id] and (
                names[other.artist_id] in target or target in names[other.artist_id]
            ):
                return headliner_is(other.artist_id, 0.9, f"info note: {s.info!r}")

    # 2. A tour name containing another act's name.
    for s in sets:
        t = norm(s.tour)
        if not t:
            continue
        for other in sets:
            n = names[other.artist_id]
            if other.artist_id != s.artist_id and n and re.search(rf"\b{re.escape(n)}\b", t):
                return headliner_is(other.artist_id, 0.75, f"tour name {s.tour!r}")

    # 3. Exactly one act lists a tour.
    with_tour = [s for s in sets if s.tour]
    if len(with_tour) == 1:
        return headliner_is(with_tour[0].artist_id, 0.6, f"only act with a tour ({with_tour[0].tour!r})")

    # 4. Clearly the longest set.
    ranked = sorted(sets, key=lambda s: -s.song_count)
    top, second = ranked[0], ranked[1]
    if top.song_count >= 8 and top.song_count >= 1.4 * max(second.song_count, 1):
        return headliner_is(top.artist_id, 0.5, f"longest set ({top.song_count} vs {second.song_count} songs)")

    return {s.artist_id: Role("unknown", 0.0, "no signal") for s in sets}
