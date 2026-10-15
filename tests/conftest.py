import json
from pathlib import Path

import pytest

from concertdb import db
from concertdb.http import Response, content_hash

FIXTURES = Path(__file__).parent / "fixtures"

OSEES = "194272cc-dcc8-4640-a4a6-66da7d250d5c"
FLAT_WORMS = "5f647289-9ff8-494d-a349-7a388307ddde"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "ledger.sqlite")
    yield c
    c.close()


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def ok(text: str, etag: str | None = None) -> Response:
    return Response(200, text, etag, None, content_hash(text))


def status(code: int) -> Response:
    return Response(code, "", None, None, None)


def add_graph_artist(conn, mbid, name, typ="group"):
    from concertdb.normalize import norm
    conn.execute(
        "INSERT INTO artists (id, name, name_norm, mbid, type, in_graph) VALUES (?, ?, ?, ?, ?, 1)",
        (mbid, name, norm(name), mbid, typ),
    )


class FakeSetlistClient:
    """Serves canned pages: pages[(kind, ident)] = [Response for page 1, page 2, ...]."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def page(self, kind, ident, page):
        self.calls.append((kind, ident, page))
        responses = self.pages.get((kind, ident))
        if responses is None:
            return status(404)
        return responses[min(page, len(responses)) - 1]


def setlist_page(setlists, total=None, page=1, per_page=20):
    return ok(json.dumps({"type": "setlists", "itemsPerPage": per_page, "page": page,
                          "total": total if total is not None else len(setlists), "setlist": setlists}))
