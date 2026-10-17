# Architecture

## Data model (`concertdb/db.py`)

```
artists   id = MusicBrainz ID, or 'name:<normalized>' when not matched to the graph
venues    + venue_source_ids (each source's own venue id → our venue)
shows     one per venue per night (dates ±1 day merge: past-midnight shows)
claims    what ONE source says about ONE artist at ONE show:
          role (headliner/opener/unknown), position, confidence, url, extra JSON
billings  the resolved bill, recomputed from all claims for a show
crawl_state  one row per unit of work: next_due, adaptive interval, cursor, etag, hash
usage     requests and $ per source per day (budgets, caps)
```

Sources only ever add claims. `ledger.resolve_show()` turns claims into
billings: each source's vote is weighted (`SOURCE_WEIGHTS`: manual 1.0,
schema.org 0.9, setlist.fm 0.8, LLM 0.6), agreeing sources raise confidence
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
- Every response is archived (`data/raw/<source>/<day>.jsonl.gz`) so parsers
  and prompts can be re-run without refetching.

## Rate limits

setlist.fm requests start at least 1 s apart. On HTTP 429 the crawler waits
(the `Retry-After` header if given, else 10 s, 30 s, 60 s, 120 s), slows to
1.5× the interval for the rest of the run (capped at 5 s), and retries the
same page. Five 429s in a row is treated as the daily limit and ends the
run; the cursor resumes from that page next time.

## Concurrency

Two processes share the database: the daily `concertdb run` (pages,
setlist.fm, backup) and the long-running `concertdb pollstar`. SQLite in WAL
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
  pages, seed) and `ledger.pollstar.lock` (Pollstar). `run` skips its Pollstar
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
  not asked again (misses are retried after a year)
- event lists are rechecked every 3 days, backing off to 14 days when the
  list is unchanged (hash over all pages); unchanged lists write nothing
- any venue with a show in the last 3 years is queued for lookup, busiest first

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
4. **Search + Claude for gaps**: narrow questions ("who opened for X at Y on
   date Z") answered from fetched pages; Haiku via the Batch API, cached by
   (page hash, prompt version), hard monthly $ cap from `usage`.
5. **Bandsintown** past events, if an `app_id` is granted.
6. **MusicSceneMap import**: load `opened_for.csv` into the graph build. The
   ledger's saved Pollstar events could also serve MusicSceneMap's concert
   recommendations, so it calls Pollstar less.
