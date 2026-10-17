"""HTTP fetching with pacing, conditional requests, and a raw-response archive.

Every response body is appended to data/raw/<source>/<YYYY-MM-DD>.jsonl.gz so
pages can be re-parsed later (better parser, better prompt) without fetching
them again. The archive is backed up with the database.
"""

import gzip
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

USER_AGENT = "concert-database/0.1 (+https://github.com/rahuljaisimha/concert-database)"


@dataclass
class Response:
    status: int
    text: str
    etag: str | None
    last_modified: str | None
    content_hash: str | None
    retry_after: float | None = None    # seconds, from a 429/503 Retry-After header
    url: str | None = None              # final URL, after redirects

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    def json(self):
        return json.loads(self.text)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None  # HTTP-date form: ignore, the caller's backoff applies


class Fetcher:
    def __init__(self, data_dir: Path | str, min_interval: float = 0.0, session=None, keep_raw: bool = True):
        self.keep_raw = keep_raw    # False: don't archive responses (see archive())
        self.data_dir = Path(data_dir)
        self.min_interval = min_interval
        self.session = session or requests.Session()
        # Not setdefault: requests.Session already has a default ("python-requests/x.y"),
        # which many sites block outright (HTTP 403).
        self.session.headers["User-Agent"] = USER_AGENT
        self._last = 0.0

    def _pace(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(
        self,
        source: str,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        timeout: float = 30,
        keep_raw: bool | None = None,
    ) -> Response:
        """keep_raw=False skips the raw archive for this response (the caller may
        archive it later with archive(), e.g. only if it turned out to be new)."""
        h = dict(headers or {})
        if etag:
            h["If-None-Match"] = etag
        if last_modified:
            h["If-Modified-Since"] = last_modified
        self._pace()
        r = self.session.get(url, params=params, headers=h, timeout=timeout)
        text = r.text if r.status_code != 304 else ""
        resp = Response(
            status=r.status_code,
            text=text,
            etag=r.headers.get("ETag"),
            last_modified=r.headers.get("Last-Modified"),
            content_hash=content_hash(text) if r.status_code == 200 else None,
            retry_after=_retry_after(r.headers.get("Retry-After")),
            url=r.url,
        )
        if r.status_code == 200 and (self.keep_raw if keep_raw is None else keep_raw):
            self.archive(source, r.url, params, text)
        return resp

    def post_json(
        self,
        source: str,
        url: str,
        body: dict,
        headers: dict | None = None,
        timeout: float = 120,
    ) -> Response:
        """POST a JSON body. Successful responses are archived with the request body."""
        self._pace()
        r = self.session.post(url, json=body, headers=headers or {}, timeout=timeout)
        resp = Response(
            status=r.status_code,
            text=r.text,
            etag=None,
            last_modified=None,
            content_hash=content_hash(r.text) if r.status_code == 200 else None,
            retry_after=_retry_after(r.headers.get("Retry-After")),
        )
        if r.status_code == 200:
            self.archive(source, r.url, body, r.text)
        return resp

    def archive(self, source: str, url: str, params: dict | None, body: str) -> None:
        now = datetime.now(timezone.utc)
        d = self.data_dir / "raw" / source
        d.mkdir(parents=True, exist_ok=True)
        rec = {"fetched_at": now.isoformat(timespec="seconds"), "url": url, "params": params, "body": body}
        # Appending gzip members to one file is valid gzip; readers see one stream.
        with gzip.open(d / f"{now.date().isoformat()}.jsonl.gz", "at", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
