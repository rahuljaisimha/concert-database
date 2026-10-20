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
