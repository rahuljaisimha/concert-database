"""Backups: consistent snapshots of the ledger plus the raw archive, to Cloudflare R2.

Local: data/backups/ledger-<UTC timestamp>.sqlite.gz, newest 7 kept.
Remote (if R2_* env vars are set), bucket layout:
    snapshots/ledger-<ts>.sqlite.gz   newest 7, plus the newest of each of the last 8 weeks
    raw/<source>/<day>.jsonl.gz       the raw response archive (re-uploaded while it grows)

Env: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET.
Needs boto3 (pip install 'concertdb[r2]').
"""

import gzip
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

KEEP_LOCAL = 7
KEEP_DAILY = 7
KEEP_WEEKLY = 8
# Days of raw archive kept on this machine (data/raw/<source>/<day>.jsonl.gz);
# CONCERTDB_RAW_KEEP_DAYS overrides, 0 keeps everything. The R2 copy has its own
# 30-day lifecycle rule on raw/ (docs/HOSTING.md).
RAW_KEEP_DAYS = 30


def raw_keep_days() -> int:
    v = os.environ.get("CONCERTDB_RAW_KEEP_DAYS")
    return int(v) if v not in (None, "") else RAW_KEEP_DAYS


def prune_raw(data_dir: Path, keep_days: int, today=None, log=print) -> int:
    """Delete day files of the raw archive older than keep_days. Returns how many."""
    if keep_days <= 0:
        return 0
    from datetime import date, timedelta
    cutoff = ((today or datetime.now(timezone.utc).date()) - timedelta(days=keep_days)).isoformat()
    raw = Path(data_dir) / "raw"
    manifest_path = Path(data_dir) / "backups" / "uploaded.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    removed = 0
    for f in sorted(raw.glob("*/*.jsonl.gz")) if raw.exists() else []:
        day = f.name.removesuffix(".jsonl.gz")
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        if day < cutoff:
            f.unlink()
            manifest.pop(f.relative_to(data_dir).as_posix(), None)
            removed += 1
    if removed:
        if manifest_path.exists():
            manifest_path.write_text(json.dumps(manifest, indent=0, sort_keys=True))
        log(f"removed {removed} raw archive file(s) older than {keep_days} days")
    return removed


def snapshot(conn: sqlite3.Connection, data_dir: Path) -> Path:
    """Consistent copy via SQLite's backup API (safe while the crawler runs)."""
    out_dir = data_dir / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fd, tmp = tempfile.mkstemp(suffix=".sqlite", dir=out_dir)
    os.close(fd)
    try:
        dst = sqlite3.connect(tmp)
        conn.backup(dst)
        dst.close()
        out = out_dir / f"ledger-{ts}.sqlite.gz"
        with open(tmp, "rb") as src, gzip.open(out, "wb", compresslevel=6) as gz:
            shutil.copyfileobj(src, gz)
    finally:
        Path(tmp).unlink(missing_ok=True)
    for old in sorted(out_dir.glob("ledger-*.sqlite.gz"))[:-KEEP_LOCAL]:
        old.unlink()
    return out


def keep_remote(keys: list[str]) -> set[str]:
    """Which snapshot keys to keep: newest KEEP_DAILY, plus newest per ISO week for KEEP_WEEKLY weeks."""
    snaps = sorted(keys, reverse=True)
    keep = set(snaps[:KEEP_DAILY])
    weeks: dict[tuple[int, int], str] = {}
    for k in snaps:
        stamp = Path(k).name.removeprefix("ledger-").removesuffix(".sqlite.gz")
        try:
            dt = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
        except ValueError:
            continue
        weeks.setdefault(tuple(dt.isocalendar())[:2], k)
    keep.update(sorted(weeks.values(), reverse=True)[:KEEP_WEEKLY])
    return keep


def r2_configured() -> bool:
    return all(os.environ.get(k) for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"))


def _r2():
    import boto3  # optional dependency

    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    ), os.environ["R2_BUCKET"]


def _list(s3, bucket: str, prefix: str) -> list[str]:
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys += [o["Key"] for o in page.get("Contents", [])]
    return keys


def upload(snapshot_path: Path, data_dir: Path, log=print) -> None:
    s3, bucket = _r2()
    s3.upload_file(str(snapshot_path), bucket, f"snapshots/{snapshot_path.name}")
    log(f"uploaded snapshots/{snapshot_path.name}")

    # Raw archive: upload files that are new or have grown since last time.
    manifest_path = data_dir / "backups" / "uploaded.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    raw = data_dir / "raw"
    n = 0
    for f in sorted(raw.rglob("*.jsonl.gz")) if raw.exists() else []:
        rel = f.relative_to(data_dir).as_posix()
        size = f.stat().st_size
        if manifest.get(rel) == size:
            continue
        s3.upload_file(str(f), bucket, rel)
        manifest[rel] = size
        n += 1
    manifest_path.write_text(json.dumps(manifest, indent=0, sort_keys=True))
    log(f"uploaded {n} raw archive file(s)")

    snaps = _list(s3, bucket, "snapshots/")
    keep = keep_remote(snaps)
    for k in snaps:
        if k not in keep:
            s3.delete_object(Bucket=bucket, Key=k)


def restore(db_path: Path, data_dir: Path, log=print) -> Path:
    """Download the newest snapshot and the raw archive. Refuses to overwrite a database."""
    if db_path.exists():
        raise FileExistsError(f"{db_path} exists; move it aside before restoring")
    s3, bucket = _r2()
    snaps = sorted(_list(s3, bucket, "snapshots/"))
    if not snaps:
        raise FileNotFoundError("no snapshots in the bucket")
    latest = snaps[-1]
    tmp = data_dir / "backups" / Path(latest).name
    tmp.parent.mkdir(parents=True, exist_ok=True)
    s3.download_file(bucket, latest, str(tmp))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(tmp, "rb") as src, open(db_path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    log(f"restored {latest} → {db_path}")
    for key in _list(s3, bucket, "raw/"):
        dest = data_dir / key
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(dest))
    return db_path
