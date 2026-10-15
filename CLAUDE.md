# CLAUDE.md

A long-running crawler that builds a ledger of concert bills (who opened for
whom) for MusicSceneMap. Python 3.10+, SQLite, no web framework.

Read README.md for usage and docs/ARCHITECTURE.md for the data model, the
opener inference rules, and how refetching is avoided. Keep both accurate when
a change affects what they describe.

## Layout

- `concertdb/db.py` — schema (STRICT tables, Postgres-portable types)
- `concertdb/ledger.py` — artists, venues, shows, claims → billings
- `concertdb/infer.py` — headliner/opener from per-artist setlists (pure)
- `concertdb/state.py` — crawl_state scheduling, backoff, usage budgets
- `concertdb/http.py` — fetching, conditional requests, raw archive
- `concertdb/sources/` — one module per source (setlistfm, jsonld, ...)
- `concertdb/seed.py`, `export.py`, `backup.py`, `status.py`, `cli.py`

## Rules

- Sources add claims via `ledger.add_claim`; never write `billings` directly.
- Every network source registers units of work in `crawl_state` and records
  requests/spend in `usage`. Respect each source's rate limit and daily cap.
- Keep SQL portable (see "Moving to Postgres later" in docs/ARCHITECTURE.md).
- Tests are offline. New parsers get a fixture in `tests/fixtures/` (say in its
  README whether it was captured or hand-written).

## Workflow

- Every change goes through a pull request into `main`.
- Before pushing: `.venv/bin/pytest`.
- Never commit `data/` or `.env`.
