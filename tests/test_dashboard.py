import json
from datetime import date

import pytest

from concertdb import cli, dashboard, ledger, runs
from conftest import add_graph_artist


def show(conn, day, venue, acts, country="US"):
    v = ledger.get_or_create_venue(conn, venue, "Los Angeles", country=country)
    s = ledger.find_or_create_show(conn, day, v)
    for pos, a in enumerate(acts):
        aid = a if a.startswith("mb") else ledger.get_or_create_artist(conn, a)
        ledger.add_claim(conn, s, aid, "setlistfm", f"{day}-{venue}", role="headliner" if pos == 0 else "opener",
                         confidence=0.9, position=pos)
    conn.commit()
    return s


@pytest.fixture
def ledger_with_shows(conn):
    add_graph_artist(conn, "mb-idc", "Iguana Death Cult")
    add_graph_artist(conn, "mb-fw", "Flat Worms")
    show(conn, "2024-09-17", "Zebulon", ["mb-idc", "mb-fw"])
    show(conn, "2024-09-18", "Teragram", ["mb-idc", "mb-fw", "Some Local Band"])
    show(conn, "2025-01-02", "Echo", ["mb-fw"])
    show(conn, "2030-05-05", "Moth Club", ["mb-fw"], country="UK")
    return conn


def test_business_numbers(ledger_with_shows):
    d = dashboard.data_metrics(ledger_with_shows, today=date(2026, 10, 20))
    assert d["concerts"] == 4 and d["upcoming"] == 1 and d["past"] == 3
    assert {x["label"]: x["concerts"] for x in d["lineup_sizes"]} == \
        {"0 acts": 0, "1 act": 2, "2 acts": 1, "3 acts": 1, "4+ acts": 0}
    assert d["concerts_2plus"] == 2 and d["concerts_with_opener"] == 2
    assert d["artists_with_concerts"] == 3 and d["artists_with_concerts_mb"] == 2
    assert d["headliners"] == 2 and d["openers"] == 2
    assert d["opened_for_pairs"] == 1                   # Flat Worms -> Iguana Death Cult; not the unmatched band
    assert d["venues_with_concerts"] == 4 and d["countries"] == 2
    assert d["top_countries"][0] == {"label": "US", "concerts": 3}
    assert [x["label"] for x in d["by_year"]][0] == "2024" and d["by_year"][-1]["future"]
    assert d["by_source"][0]["source"] == "setlistfm" and d["added_7d"] == 4


def test_runs_are_recorded_even_when_they_fail(conn):
    assert runs.record(conn, "setlistfm", lambda: {"requests": 3, "_streak": 1}) == {"requests": 3, "_streak": 1}
    with pytest.raises(ValueError):
        runs.record(conn, "gemini", lambda: (_ for _ in ()).throw(ValueError("boom")))
    gem, sfm = runs.recent(conn)
    assert sfm["ok"] == 1 and sfm["stats"] == {"requests": 3}         # internal keys dropped
    assert gem["ok"] == 0 and gem["stats"]["error"] == "ValueError: boom" and gem["finished_at"]


def test_dashboard_page_and_trend(ledger_with_shows, tmp_path):
    conn = ledger_with_shows
    runs.record(conn, "gemini", lambda: {"shows": 5, "specific": 2, "not_found": 3, "contextdev_balance": 49_000})
    conn.execute("INSERT INTO metric_snapshots VALUES ('2000-01-01T00:00:00+00:00', ?)",
                 (json.dumps({k: 1 for k in dashboard.HEADLINE}),))
    conn.commit()
    info = dashboard.collect(conn)
    assert info["data"]["deltas"]["week"]["concerts"] == 3          # 4 now, 1 in the old snapshot
    assert info["runs"]["contextdev"]["balance"] == 49_000
    assert info["runs"]["last"]["gemini"]["stats"]["specific"] == 2
    levels = {a["level"] for a in info["alerts"]}
    assert "warning" in levels                                      # no backup recorded yet
    out = tmp_path / "d.html"
    dashboard.write(conn, [out])
    html = out.read_text()
    assert "/*DATA*/" not in html and '"concerts": 4' in html and "<title>Concert database</title>" in html
    # One per write (two in the same second share a row).
    assert conn.execute("SELECT COUNT(*) FROM metric_snapshots").fetchone()[0] in (2, 3)


def test_cli_backup_and_dashboard(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CONCERTDB_DASHBOARD", raising=False)
    dbp = str(tmp_path / "ledger.sqlite")
    cli.main(["--db", dbp, "add-show", "--date", "2026-09-17", "--venue", "Zebulon",
              "--city", "Los Angeles", "--headliner", "Iguana Death Cult", "--opener", "Flat Worms"])
    cli.main(["--db", dbp, "backup"])
    second = tmp_path / "copy" / "dash.html"
    monkeypatch.setenv("CONCERTDB_DASHBOARD", str(second))
    cli.main(["--db", dbp, "dashboard"])
    assert (tmp_path / "dashboard.html").exists() and second.exists()
    info = dashboard.collect(cli._conn(cli.build_parser().parse_args(["--db", dbp, "status"])), snapshot=False)
    assert info["runs"]["backup"]["stale"] is False and info["runs"]["last"]["backup"]["stats"]["bytes"] > 0


class FakeS3:
    def __init__(self):
        self.uploads = []

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.uploads.append((open(path).read(), bucket, key, ExtraArgs))


def test_dashboard_goes_to_r2_when_configured(tmp_path, monkeypatch, capsys):
    from concertdb import backup
    s3 = FakeS3()
    monkeypatch.setattr(backup, "_r2", lambda: (s3, "concert-database"))
    monkeypatch.delenv("CONCERTDB_DASHBOARD", raising=False)
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.setenv(k, "x")
    dbp = str(tmp_path / "ledger.sqlite")
    cli.main(["--db", dbp, "dashboard"])
    (html, bucket, key, extra), = s3.uploads
    assert key == "dashboards/index.html" and bucket == "concert-database"
    assert html.startswith("<!doctype html>") and extra["ContentType"].startswith("text/html")
    cli.main(["--db", dbp, "dashboard", "--no-upload"])
    assert len(s3.uploads) == 1


def test_a_failed_upload_never_fails_a_run(tmp_path, monkeypatch, capsys):
    from concertdb import backup
    monkeypatch.setattr(backup, "r2_configured", lambda: True)
    monkeypatch.setattr(backup, "upload_dashboard", lambda p: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.delenv("SETLISTFM_API_KEY", raising=False)
    cli.main(["--db", str(tmp_path / "ledger.sqlite"), "run", "--no-backup", "--pollstar-requests", "0"])
    assert "dashboard problem (the run carries on): OSError: offline" in capsys.readouterr().out
    assert (tmp_path / "dashboard.html").exists()



def test_a_step_still_running_is_told_apart_from_a_killed_one(tmp_path):
    from concertdb import db
    conn = db.connect(tmp_path / "ledger.sqlite")
    lock = tmp_path / "ledger.pollstar.lock"
    lock.write_text("")
    conn.execute("INSERT INTO runs (step, started_at) VALUES ('pollstar', '2000-01-01T00:00:00+00:00')")
    conn.execute("INSERT INTO runs (step, started_at) VALUES ('gemini', '2000-01-01T00:00:00+00:00')")
    conn.commit()
    proc = tmp_path / "locks"
    proc.write_text(f"1: FLOCK  ADVISORY  WRITE 7209 08:20:{lock.stat().st_ino} 0 EOF\n"
                    "2: POSIX  ADVISORY  WRITE 99 08:20:1 0 EOF\n")
    assert dashboard.held_locks(conn, str(proc)) == {"pollstar"}
    import concertdb.dashboard as d
    real = d.held_locks
    d.held_locks = lambda c: real(c, str(proc))
    try:
        info = dashboard.collect(conn, snapshot=False)
    finally:
        d.held_locks = real
    assert info["runs"]["last"]["pollstar"]["running"] is True
    assert info["runs"]["last"]["gemini"]["running"] is False
    texts = [a["text"] for a in info["alerts"]]
    assert "Last gemini run stopped without finishing (killed?)" in texts
    assert not any("pollstar run stopped" in t for t in texts)


def test_gemini_calls_by_model(conn, monkeypatch):
    from concertdb import state
    from concertdb.sources import gemini
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.setenv("GEMINI_MODELS", "model-a,model-b")
    day = state.usage_day("gemini").isoformat()
    for src, n in (("gemini", 700), ("gemini:model-a", 500), ("gemini:model-b", 150)):
        conn.execute("INSERT INTO usage (day, source, requests) VALUES (?, ?, ?)", (day, src, n))
    show_id = show(conn, "2024-09-17", "Zebulon", ["Iguana Death Cult"])
    aid = ledger.get_or_create_artist(conn, "Flat Worms")
    ledger.add_claim(conn, show_id, aid, "gemini", "show:1", role="opener", confidence=0.8,
                     extra={"model": "model-b", "check": "specific"})
    conn.commit()
    runs.record(conn, "gemini", lambda: {"gemini_calls": 650, "calls_by_model": {"model-a": 500, "model-b": 150},
                                         "tokens_by_model": {"model-a": 3_000_000, "model-b": 900_000}})
    gm = dashboard.collect(conn, snapshot=False)["runs"]["gemini_models"]
    assert gm["models"] == ["model-a", "model-b"] and gm["has_earlier"]
    today = gm["days"][-1]
    assert (today["model-a"], today["model-b"], today["_earlier"]) == (500, 150, 50)
    a, b = gm["week"]
    assert (a["calls"], a["tokens"], a["tokens_per_call"]) == (500, 3_000_000, 6000)
    assert (b["calls"], b["specific"], b["page"]) == (150, 1, 0)
    assert dashboard.collect(conn, snapshot=False)["runs"]["limits"]["gemini"] == 2 * gemini.DAILY_LIMIT


def test_prioritized_artists_in_queue_order(ledger_with_shows):
    conn = ledger_with_shows
    from concertdb import seed, state
    from concertdb.sources import setlistfm
    add_graph_artist(conn, "mb-a", "Psychedelic Porn Crumpets")
    add_graph_artist(conn, "mb-b", "Fomies")
    seed.prioritize(conn, ["mb-a", "mb-b", "mb-fw"])
    conn.execute("UPDATE crawl_state SET next_due = '2026-10-01T00:00:00+00:00' WHERE key = 'artist:mb-b'")
    state.save_cursor(conn, setlistfm.SOURCE, "artist:mb-a", {"next_page": 3})
    conn.execute("UPDATE crawl_state SET last_checked = '2026-10-25T02:00:00+00:00' WHERE key = 'artist:mb-a'")
    state.save_cursor(conn, setlistfm.SOURCE, "artist:mb-fw", {"backfill_done": True})
    conn.execute("UPDATE crawl_state SET last_checked = '2026-10-24T02:00:00+00:00' WHERE key = 'artist:mb-fw'")
    state.ensure(conn, setlistfm.SOURCE, "artist:mb-idc")          # an ordinary unit: not listed
    conn.commit()
    p = dashboard.prioritized(conn)
    assert p["waiting"] == 2 and p["done"] == 1
    assert [x["name"] for x in p["next"]] == ["Fomies", "Psychedelic Porn Crumpets"]   # due first, first in line
    assert p["next"][1]["next_page"] == 3 and p["next"][0]["last_checked"] is None
    assert [(x["name"], x["concerts"]) for x in p["recent"]] == [("Flat Worms", 4)]
    assert dashboard.prioritized(conn, shown=1)["next"][0]["name"] == "Fomies"
    html = dashboard.render(dashboard.collect(conn, snapshot=False))
    assert 'c.id = "prioritized"' in html and "Psychedelic Porn Crumpets" in html


def test_coverage_parts_and_overall(ledger_with_shows):
    conn = ledger_with_shows
    from concertdb import state
    from concertdb.sources import setlistfm
    for mbid in ("mb-idc", "mb-fw", "mb-x"):
        state.ensure(conn, setlistfm.SOURCE, f"artist:{mbid}")
    conn.execute("UPDATE crawl_state SET last_checked = '2026-10-20T00:00:00+00:00', cursor = '{\"backfill_done\": true}'"
                 " WHERE key = 'artist:mb-fw'")
    conn.execute("UPDATE crawl_state SET last_checked = '2026-10-20T00:00:00+00:00' WHERE key = 'artist:mb-idc'")
    setlistfm.record_total(conn, "artist:mb-fw", 8)      # holds 4 (show() adds setlistfm claims)
    setlistfm.record_total(conn, "artist:mb-idc", 1)     # holds 2: capped at its total
    state.ensure(conn, "gemini", "show:1"); state.ensure(conn, "gemini", "show:2")
    conn.execute("UPDATE crawl_state SET last_checked = '2026-10-20T00:00:00+00:00' WHERE key = 'show:1'")
    conn.commit()
    cov = dashboard.coverage(conn, "2026-10-20")
    p = {x["key"]: x for x in cov["parts"]}
    assert (p["artists_checked"]["num"], p["artists_checked"]["den"]) == (2, 3)
    assert (p["artists_full"]["num"], p["artists_full"]["den"]) == (1, 3)
    assert (p["history_held"]["num"], p["history_held"]["den"]) == (5, 9)
    assert (p["bills_settled"]["num"], p["bills_settled"]["den"]) == (2, 3)    # past: 2 two-act shows of 3
    assert (p["gemini_checked"]["num"], p["gemini_checked"]["den"]) == (1, 2)
    assert cov["overall"] == round(100 * 2 / 3 * 5 / 9 * 2 / 3, 2)
    info = dashboard.collect(conn, today=date(2026, 10, 20), snapshot=True)
    assert info["data"]["coverage_pct"] == cov["overall"]
    trend = info["data"]["coverage_trend"]
    assert len(trend) == 1 and trend[0]["overall"] == cov["overall"]
    assert trend[0]["parts"]["history_held"] == [5, 9]          # counts kept, not just percentages
    stored = json.loads(conn.execute("SELECT metrics FROM metric_snapshots").fetchone()[0])
    assert stored["coverage"]["artists_checked"] == [2, 3]
    assert "coverage" not in info["data"]["trend"][0]           # the growth charts' data stays as it was
    html = dashboard.render(info)
    assert "How complete is it?" in html and "Completeness over time" in html and '"num_label"' in html


def test_coverage_without_any_totals_has_no_overall(conn):
    assert dashboard.coverage(conn, "2026-10-20")["overall"] is None
