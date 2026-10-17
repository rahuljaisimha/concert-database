# Architecture

## Data model (`concertdb/db.py`)

```
artists   id = MusicBrainz ID, or 'name:<normalized>' when not matched to the graph
venues    + venue_source_ids (each source's own venue id → our venue)
shows     one per venue per night (see "Which records are the same show")
claims    what ONE source says about ONE artist at ONE show:
          role (headliner/opener/unknown), position, confidence, url, extra JSON
billings  the resolved bill, recomputed from all claims for a show
crawl_state  one row per unit of work: next_due, adaptive interval, cursor, etag, hash
usage     requests and $ per source per day (budgets, caps; Gemini's day is
          Pacific time, when its free quota resets)
```

### Which records are the same show

`ledger.find_or_create_show()` decides which show a source's record belongs
to (an event from Pollstar or a venue page, one setlist from setlist.fm):

- a record already stored (same source and native id) keeps its show;
- a show at the same venue on the same date is the same show;
- a show a day apart is the same show only if it shares an act: some sources
  date a show that runs past midnight to the next day, but a venue's shows on
  consecutive nights, or a festival's days, are different shows;
- never two different events from an event source (Pollstar, schema.org pages,
  manual entries), and never two different setlists by the same act (a
  residency's nights), even on the same date.

Until October 2026 any two records a day apart were merged, which joined
consecutive nights into one bill (and invented openers between them).
`concertdb repair-shows` splits shows stored that way, using each record's
own date (kept in the claim since then, or read back from the raw archive).
It is safe to run again; `--dry-run` reports without saving.

Sources only ever add claims. `ledger.resolve_show()` turns claims into
billings: each source's vote is weighted (`SOURCE_WEIGHTS`: manual 1.0,
schema.org 0.9, setlist.fm 0.8, Gemini/LLM 0.6), agreeing sources raise confidence
(1 − Π(1 − c)), disagreeing ones lower it. Bills with 8+ acts are flagged as
festivals and excluded from `opened_for`.

## Inferring openers from setlist.fm (`concertdb/infer.py`)

setlist.fm has one setlist per artist per show and no opener field. When two
or more acts have setlists for the same venue and night:

1. an info note ("Opening for X") → 0.9
2. a tour name containing another act's name → 0.75
3. only one act lists a tour → 0.6
4. one act played clearly more songs (≥8 and ≥1.4× the next) → 0.5

Otherwise roles stay unknown (the show still counts as "played together").

## Not refetching what we have (`concertdb/state.py`)

- Past shows don't change: once stored, a setlist is only re-read if its
  `lastUpdated` changes.
- First visit to an artist/venue reads its newest 10 pages (200 setlists),
  saving a cursor after every page so a run that hits the daily budget
  resumes there. The rest of its history is queued as a **deep pass** at the
  lowest priority, so every artist gets recent coverage before any artist's
  old shows are fetched; the deep pass then continues from the saved page
  until the history is complete. Artists you `prioritize` skip the cap and
  get their full history at once.
- Later visits read newest-first and stop at the first page with nothing new.
- Recheck intervals double when nothing changed (setlist.fm 14 → 180 days,
  pages 1 → 7 days, or 365 once every event on a page is past) and reset when
  something changes.
- Pages are fetched with `If-None-Match` / `If-Modified-Since`, and an unchanged
  content hash counts as unchanged.
- Responses are archived (`data/raw/<source>/<day>.jsonl.gz`) so parsers and
  prompts can be re-run without refetching, for 30 days (see "Raw archive").

## Raw archive

The archive is for re-parsing recent responses, building test fixtures and
debugging, not a permanent record: the ledger keeps every fact with its
source, and claims keep their own event date. So it is kept short:

- **30 days** on this machine (`backup.prune_raw`, at the end of each backup;
  `CONCERTDB_RAW_KEEP_DAYS`, 0 = keep everything) and in R2 (a lifecycle
  rule deletes `raw/` objects after 30 days; see docs/HOSTING.md).
- **Pollstar event lists** are archived only when they changed; an unchanged
  recheck stores nothing. Venue lookups (autocomplete) are always archived;
  `pollstar-rematch` uses them.
- **Gemini evidence pages are not archived.** The claim keeps a snippet of
  the page text around the matched name (`extra.snippet`). Gemini's own
  answers are archived (`data/raw/gemini/`).

## Rate limits

setlist.fm requests start at least 1 s apart. On HTTP 429 the crawler waits
(the `Retry-After` header if given, else 10 s, 30 s, 60 s, 120 s), slows to
1.5× the interval for the rest of the run (capped at 5 s), and retries the
same page. Five 429s in a row is treated as the daily limit and ends the
run; the cursor resumes from that page next time.

## Concurrency

Three processes share the database: the daily `concertdb run` (pages,
setlist.fm, backup) and the long-running `concertdb pollstar` and
`concertdb gemini`. SQLite in WAL
mode lets readers run alongside one writer; writers take turns. To keep the
turns short:

- **No lock is held while waiting.** Each source commits before every network
  request and every backoff sleep (`conn.commit()`), then takes the write lock
  again when the response is in (`db.begin_write`). The write lock is only
  held while a response is being stored, milliseconds at a time.
- **Find-or-create is serialized.** `db.begin_write` uses `BEGIN IMMEDIATE`,
  so looking up a venue, artist or show and inserting it if missing can't
  interleave with the other process doing the same: no duplicate venues.
- **Writers wait instead of failing**: connections wait up to 60 s
  (`db.BUSY_TIMEOUT`) for the other process's write to finish.
- **One process per kind**: lock files `ledger.crawl.lock` (run, setlist.fm,
  pages, seed), `ledger.pollstar.lock` (Pollstar) and `ledger.gemini.lock`
  (Gemini). `run` skips its Pollstar
  step while the Pollstar process holds its lock.
- Backups use SQLite's backup API, which is safe while either process writes.

Because work is committed page by page, a setlist.fm recheck that stops
part-way (budget, rate limit, crash) saves the page it reached in its cursor
(`recheck_page`) and resumes there: starting again from page 1 would find
nothing new and stop before the pages it never read.

## Moving to Postgres later

Tables are `STRICT`, dates are ISO-8601 text, booleans are 0/1 integers with
CHECK constraints, JSON is text, matching uses a `*_norm` column instead of
`COLLATE NOCASE`, and upserts use `ON CONFLICT`, which Postgres shares.
`pgloader sqlite:///data/ledger.sqlite postgresql://...` should move it as-is.
The raw archive lives outside the database, which keeps the database small.

## Pollstar (`concertdb/sources/pollstar.py`)

Pollstar only returns events from today onward, so its bills are lost unless
saved before the show. MusicSceneMap queries it only when someone asks for
concert recommendations; this crawler queries every venue the ledger knows
about, on a schedule, with tight limits:

- its own long process (`concertdb pollstar`, started daily next to `run`):
  one request a second, at most 20,000 a day (`POLLSTAR_INTERVAL`,
  `POLLSTAR_DAILY_LIMIT` in `.env`); `run` itself spends at most 300 when the
  Pollstar process isn't running
- pushback (HTTP 403, 429, 503 or a network error) waits (`Retry-After`, else
  30 s, 60 s, 120 s, 300 s, 600 s), slows to 1.5× the interval for the rest of
  the run (capped at 10 s) and retries; six in a row pauses Pollstar for 12
  hours for every process, so a block isn't made worse
- the due list is re-read every 200 units and newly found venues are queued
  for lookup every 30 minutes, so a run lasting hours picks up what setlist.fm
  found that day
- each venue's name is looked up once; the match (or the miss) is stored and
  not asked again (misses are retried after a year). Autocomplete names come
  HTML-escaped ("Stubb&#39;s", "Bar &amp; Grill") and are un-escaped before
  matching (`normalize.norm` also un-escapes, for any source);
  `concertdb pollstar-rematch` re-checks earlier misses against the archived
  answers without new requests
- event lists are fetched 1,000 events a page (the website uses 50), so a
  venue's upcoming shows are almost always one request
- event lists are rechecked every 3 days, backing off to 14 days when the
  list is unchanged (hash over all pages); unchanged lists write nothing
- any venue with a show in the last 3 years is queued for lookup, busiest first

## Gemini (`concertdb/sources/gemini.py`)

Fills in openers for shows already in the ledger: past shows with an act from
the MusicSceneMap graph and at most `GEMINI_MAX_ACTS` acts listed (default 1,
so any show with two or more acts is skipped, even one that may be missing a
second opener), best-connected artists first. One search-grounded request per
show asks for the headliner, the openers in order, and a quote from each page
that names an opener, as JSON.

The answer isn't trusted as given. A grounded response can cite pages the model
didn't read, misquote them, or come back with no grounding metadata at all, so
up to 3 cited pages are fetched (the model's evidence URLs first, then the
grounding sources; Google's redirect links resolve to the real page, whose URL
is stored on the claim) and each act's name is looked for on them:

| Check | Confidence | × weight 0.6 | Exported at the default 0.4? |
|---|---|---|---|
| name found on a fetched page | 0.8 | 0.48 | yes |
| no page could be fetched | 0.5 | 0.30 | only with another source |
| pages fetched, name on none | 0.2 | 0.12 | no |

The model saying `confident: false` multiplies these by 0.6. Each claim's
`extra` records the model, the prompt version, the check, whether the quote
itself was on a page, and how many searches the request made.

Free tier limits (per project): 1,500 search-grounded requests a day, reset at
midnight Pacific; per-minute limits are far above what this uses, so requests
go one at a time, 2 s apart, and the run takes a few hours.

- at most 1,400 requests per Pacific day (`usage`), leaving room for smoke
  tests and requests made by hand
- HTTP 429 ends the run without marking the show; it is asked next run
- a show is asked once; if nothing is found it is asked again after 90, 180,
  then 365 days; a show that no longer qualifies (another source added an
  act, or `GEMINI_MAX_ACTS` was lowered) is skipped and dropped from the
  queue, so it comes back if it qualifies again
- `concertdb gemini` is its own process with its own lock
  (`ledger.gemini.lock`), next to `run` and `pollstar`; it commits before each
  Gemini request and page fetch and holds the write lock only while storing
  (see "Concurrency")
- Gemini's answers are archived (`data/raw/gemini/`); fetched evidence pages
  are not, and the claim keeps a snippet around the matched name instead

## Roadmap

1. **Concert Archives** (concertarchives.org): fans log the shows they
   attended, usually with the full lineup, which suits older and smaller
   shows better than venue calendars. Before crawling, check its robots.txt
   and terms, and ask about its "Data Requests & API" option for bulk
   access. If crawling is allowed: by artist page, slowly, cached like
   setlist.fm (full history once, then newest-first rechecks).
2. **Wayback Machine**: archived venue calendars for past years, parsed with
   the same schema.org parser. Complements Concert Archives for venues whose
   calendars were archived but whose shows nobody logged.
3. **Gap detection**: single-act shows, tour-routing gaps (a band in Phoenix
   on the 15th and Seattle on the 22nd probably played LA in between), and
   capture–recapture estimates of missing shows per artist.
4. **More from the Gemini step**: one request per tour leg instead of per
   show (a tour announcement often names the support for every date), which
   would stretch the daily search quota further; and a second model (e.g.
   Claude via the Batch API) to read the fetched pages and confirm roles,
   cached by (page hash, prompt version), with a hard monthly $ cap from
   `usage`.
5. **Bandsintown** past events, if an `app_id` is granted.
6. **MusicSceneMap import**: load `opened_for.csv` into the graph build. The
   ledger's saved Pollstar events could also serve MusicSceneMap's concert
   recommendations, so it calls Pollstar less.
