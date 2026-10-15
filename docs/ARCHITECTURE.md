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
- First visit to an artist/venue pages through its full history, saving a
  cursor after every page so a run that hits the daily budget resumes there.
- Later visits read newest-first and stop at the first page with nothing new.
- Recheck intervals double when nothing changed (setlist.fm 14 → 180 days,
  pages 1 → 7 days, or 365 once every event on a page is past) and reset when
  something changes.
- Pages are fetched with `If-None-Match` / `If-Modified-Since`, and an unchanged
  content hash counts as unchanged.
- Every response is archived (`data/raw/<source>/<day>.jsonl.gz`) so parsers
  and prompts can be re-run without refetching.

## Moving to Postgres later

Tables are `STRICT`, dates are ISO-8601 text, booleans are 0/1 integers with
CHECK constraints, JSON is text, matching uses a `*_norm` column instead of
`COLLATE NOCASE`, and upserts use `ON CONFLICT`, which Postgres shares.
`pgloader sqlite:///data/ledger.sqlite postgresql://...` should move it as-is.
The raw archive lives outside the database, which keeps the database small.

## Roadmap

1. **Pollstar capture**: upcoming shows with supporting acts for every venue
   MusicSceneMap knows. Port of `MusicSceneMap/src/api/pollstar.ts`.
2. **Wayback Machine**: monthly snapshots of watched venue calendars from
   past years, parsed with the same schema.org parser.
3. **Gap detection**: single-act shows, tour-routing gaps (a band in Phoenix
   on the 15th and Seattle on the 22nd probably played LA in between), and
   capture–recapture estimates of missing shows per artist.
4. **Search + Claude for gaps**: narrow questions ("who opened for X at Y on
   date Z") answered from fetched pages; Haiku via the Batch API, cached by
   (page hash, prompt version), hard monthly $ cap from `usage`.
5. **Bandsintown** past events, if an `app_id` is granted.
6. **MusicSceneMap import**: load `opened_for.csv` into the graph build.
