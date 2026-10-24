import json

import pytest

from concertdb import cli, dashboard, ledger, review
from concertdb.normalize import norm
from conftest import add_graph_artist

SALIVA = "Josey Scott: The Original Voice Of Saliva"
JOSEY = "5b2a1c3e-0000-4000-8000-000000000001"


def gemini_show(conn, day, venue, headliner, acts):
    """A show with a setlist.fm headliner and Gemini's openers: acts = [(name, extra)]."""
    v = ledger.get_or_create_venue(conn, venue, "Louisville", country="US")
    s = ledger.find_or_create_show(conn, day, v)
    ledger.add_claim(conn, s, headliner, "setlistfm", f"{day}-{venue}", role="headliner", confidence=0.9, position=0)
    for name, extra in acts:
        aid = ledger.get_or_create_artist(conn, name)
        ledger.add_claim(conn, s, aid, "gemini", f"{day}-{venue}", role="opener", confidence=0.8,
                         url="https://example.com/lineup", extra={"quote": "Louder Stage 9:45 p.m.", **extra})
    conn.commit()
    return s


@pytest.fixture
def flagged_ledger(conn):
    add_graph_artist(conn, "mb-lb", "Limp Bizkit")
    add_graph_artist(conn, JOSEY, "Josey Scott", typ="person")
    s1 = gemini_show(conn, "2026-09-19", "Louder Stage", "mb-lb",
                     [(SALIVA, {"name_check": "description"}), ("Bilmuri", {})])
    s2 = gemini_show(conn, "2026-09-20", "Highway Stage", "mb-lb", [(SALIVA, {"name_check": "description"})])
    s3 = gemini_show(conn, "2026-09-04", "Pack Square Park", "mb-lb",
                     [("The Blind Shak...", {"name_check": "truncated"}), ("Earth Tongue", {})])
    return conn, (s1, s2, s3)


def claims_of(conn, aid):
    return conn.execute("SELECT * FROM claims WHERE artist_id = ?", (aid,)).fetchall()


def test_lists_flagged_names_with_their_shows(flagged_ledger):
    conn, (s1, s2, _) = flagged_ledger
    items = review.flagged(conn)
    assert [x["name"] for x in items] == [SALIVA, "The Blind Shak..."]
    saliva = items[0]
    assert saliva["flag"] == "description" and saliva["suggestion"] == "Josey Scott"
    assert [s["show_id"] for s in saliva["shows"]] == [s1, s2]
    assert saliva["shows"][0]["headliners"] == ["Limp Bizkit"]
    assert saliva["shows"][0]["url"] == "https://example.com/lineup"
    assert saliva["shows"][0]["quote"] == "Louder Stage 9:45 p.m."
    assert items[1]["suggestion"] is None
    text = review.format_list(items)
    assert 'deploy/review.sh rename "Josey Scott: The Original Voice Of Saliva" "Josey Scott"' in text


def test_keep_drops_the_flag(flagged_ledger):
    conn, _ = flagged_ledger
    aid = review.find(conn, "The Blind Shak...")["artist_id"]
    review.keep(conn, "the blind shak...")                       # matched like any name
    assert [x["name"] for x in review.flagged(conn)] == [SALIVA]
    ex = json.loads(claims_of(conn, aid)[0]["extra"])
    assert ex["name_check"] == "kept" and ex["name_check_was"] == "truncated"
    assert dashboard.data_metrics(conn)["names_to_review"] == {"description": 1}


def test_mbid_moves_every_claim_and_drops_the_old_artist(flagged_ledger):
    conn, (s1, s2, _) = flagged_ledger
    old = review.find(conn, SALIVA)["artist_id"]
    done = review.rename(conn, SALIVA, mbid=JOSEY.upper())
    assert done["to"] == "Josey Scott" and done["shows"] == 2
    assert not claims_of(conn, old)
    assert conn.execute("SELECT 1 FROM artists WHERE id = ?", (old,)).fetchone() is None
    moved = claims_of(conn, JOSEY)
    assert len(moved) == 2
    ex = json.loads(moved[0]["extra"])
    assert ex["name_check"] == "renamed" and ex["name_as_given"] == SALIVA
    roles = dict(conn.execute("SELECT artist_id, role FROM billings WHERE show_id = ?", (s1,)).fetchall())
    assert roles[JOSEY] == "opener" and old not in roles
    assert [x["name"] for x in review.flagged(conn)] == ["The Blind Shak..."]


def test_rename_to_a_name_matches_it_like_a_new_claim(flagged_ledger):
    conn, (_, _, s3) = flagged_ledger
    done = review.rename(conn, "The Blind Shak...", new_name="The Blind Shakes")
    assert done["artist_id"] == f"name:{norm('The Blind Shakes')}" and done["to"] == "The Blind Shakes"
    assert conn.execute("SELECT 1 FROM claims WHERE show_id = ? AND source = 'gemini' AND artist_id = ?",
                        (s3, done["artist_id"])).fetchone()


def test_rename_onto_an_act_the_record_already_names_merges(flagged_ledger):
    conn, (s1, _, _) = flagged_ledger
    review.rename(conn, SALIVA, new_name="Bilmuri")               # same record already claims Bilmuri at s1
    bilmuri = ledger.get_or_create_artist(conn, "Bilmuri")
    assert len(conn.execute("SELECT 1 FROM claims WHERE show_id = ? AND artist_id = ?", (s1, bilmuri)).fetchall()) == 1
    assert len(claims_of(conn, bilmuri)) == 2                     # s1 (kept the original) + s2 (moved)


def test_delete_removes_the_claims(flagged_ledger):
    conn, (s1, s2, _) = flagged_ledger
    old = review.find(conn, SALIVA)["artist_id"]
    done = review.delete(conn, SALIVA)
    assert done["shows"] == 2 and done.get("shows_dropped") == 0  # headliners keep both shows
    assert not claims_of(conn, old)
    assert conn.execute("SELECT COUNT(*) FROM billings WHERE show_id = ?", (s2,)).fetchone()[0] == 1


def test_errors(flagged_ledger):
    conn, _ = flagged_ledger
    with pytest.raises(review.ReviewError, match="no flagged act"):
        review.keep(conn, "Bilmuri")
    with pytest.raises(review.ReviewError, match="not a MusicBrainz ID"):
        review.rename(conn, SALIVA, mbid="josey")
    with pytest.raises(review.ReviewError, match="same artist"):
        review.rename(conn, SALIVA, new_name=SALIVA)
    assert len(review.flagged(conn)) == 2


def test_cli(flagged_ledger, tmp_path, capsys, monkeypatch):
    conn, _ = flagged_ledger
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.delenv("CONCERTDB_DASHBOARD", raising=False)
    cli.main(["--db", db_path, "review"])
    assert "2 flagged act name(s)" in capsys.readouterr().out
    cli.main(["--db", db_path, "review", "keep", "The Blind Shak...", "--no-dashboard"])
    out = capsys.readouterr().out
    assert "kept 'The Blind Shak...'" in out and "1 flagged name(s) left" in out
    with pytest.raises(SystemExit, match="no flagged act"):
        cli.main(["--db", db_path, "review", "delete", "Nobody", "--no-dashboard"])


def test_dashboard_links_the_alert_to_the_list(flagged_ledger):
    conn, _ = flagged_ledger
    info = dashboard.collect(conn, snapshot=False)
    alert = [a for a in info["alerts"] if a.get("href")]
    assert alert and alert[0]["href"] == "#review" and alert[0]["text"].startswith("2 flagged")
    assert [x["name"] for x in info["review"]] == [SALIVA, "The Blind Shak..."]
    html = dashboard.render(info)
    assert 'class: "card review-card", id: "review"' in html and "deploy/review.sh" in html
