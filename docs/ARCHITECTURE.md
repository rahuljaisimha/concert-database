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
  recheck stores nothing. Venue lookups (autocomplete) are always archived.
- **Pages fetched for Gemini are not archived.** A `specific` claim keeps its
  quote (`extra.quote`). Search results and Gemini's answers are archived
  (`data/raw/firecrawl/`, `data/raw/gemini/`).

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

- its own long process (`concertdb pollstar`, started daily next to `run`),
  at most 20,000 requests a day (`POLLSTAR_DAILY_LIMIT`); `run` itself spends
  at most 300 when the Pollstar process isn't running
- the pace adapts and is remembered between runs (`meta.pollstar_interval`).
  At one request a second Pollstar answered HTTP 403 after ~400–550 requests
  (~9 minutes), two nights running. Now requests start 6 s apart
  (`POLLSTAR_INTERVAL`, also the floor); pushback (HTTP 403, 429, 503 or a
  network error) doubles the interval (up to 120 s), waits (`Retry-After`, else
  30 s, 60 s, 120 s, 300 s, 600 s) and retries; 300 clean requests in a row
  take 20% off the interval, down to the floor
- six pushbacks in a row pause Pollstar for every process, so a block isn't
  made worse: 12 hours, doubling each time it happens again (up to a week),
  back to 12 hours after a run with 300 clean requests
- the due list is re-read every 200 units and newly found venues are queued
  for lookup every 30 minutes, so a run lasting hours picks up what setlist.fm
  found that day
- each venue's name is looked up once; the match (or the miss) is stored and
  not asked again (misses are retried after a year). Autocomplete names come
  HTML-escaped ("Stubb&#39;s", "Bar &amp; Grill") and are un-escaped before
  matching (`normalize.norm` also un-escapes, for any source)
- event lists are fetched 1,000 events a page (the website uses 50), so a
  venue's upcoming shows are almost always one request
- event lists are rechecked every 3 days, backing off to 14 days when the
  list is unchanged (hash over all pages); unchanged lists write nothing
- any venue with a show in the last 3 years is queued for lookup, busiest first

## Gemini (`concertdb/sources/gemini.py`)

Fills in openers for shows already in the ledger: past, non-festival shows with
an act from the MusicSceneMap graph and at most `GEMINI_MAX_ACTS` acts listed
(default 1). Shows by artists you prioritized (`concertdb prioritize`) come
first, then the most recent shows (pages about them are easiest to find).

Gemini's own Google Search grounding isn't on the free tier for current models,
so the search is done here and Gemini (default `gemini-3.5-flash-lite`, no
tools) only reads text we fetched. Search goes to a local SearXNG
(`SEARXNG_URL`, see below) while it answers, else to Firecrawl's search API.
Work goes a tour at a time:

1. The most urgent due show, plus the same artist's other due shows that chain
   to it with gaps of at most 14 days (up to 12 shows), is one tour.
2. Two searches: the tour (`Ty Segall Australia 2026 tour support`) and its
   venues (`Ty Segall "Mo's Desert Clubhouse" "Princess Theatre" ... 2026`;
   this finds the promoter's tour page without knowing the promoter). A one-show
   tour gets one show search instead.
3. The result pages are fetched once; only the text around each show's venue,
   city and date goes into ONE Gemini call for the whole tour, which answers per
   show: headliner, openers, and a quote naming THIS show with its openers.
4. A show is done only when that quote is on a fetched page (or a search
   result), names the show (its venue, or its date and city; the date at a
   residency), doesn't state another year or weekday, and contains every
   opener. A support act announced "on all dates" isn't proof for each date,
   so a show that isn't done gets its own search and Gemini call (at most 4
   per tour).
5. Festivals: Gemini says whether each show was a festival (setlist.fm often
   lists a festival set as a one-act show). A festival, or more than 6
   openers, is kept as a lineup, checked like any bill, with
   `extra.festival` on its claims; that marks the show `is_festival` (as does a
   Pollstar `isFestival` event, or 8+ acts), so it isn't exported as
   opened-for, and it isn't asked again. How to use lineups is for later.
6. Shows by the same artist that the pages list but the ledger lacks
   (`other_shows`) are added when their quote is on a fetched page, names the
   venue and the date, and pins the year down (states it, or the right
   weekday: tour listings often omit the year, and the same listing would
   otherwise be filed under every year the artist toured then); the artist is
   named in the quote or at the top of the page; within the tour's dates ± 14
   days; and the artist has no show that date (or a day off in the same city).
   Claim key `found:<artist>:<date>`, `extra.check` = `discovered`.

| Check | Confidence | × weight 0.6 | Exported at the default 0.4? |
|---|---|---|---|
| act in a show-specific quote found on a page (`specific`) | 0.8 | 0.48 | yes |
| act named on a fetched page that names the show (`page`) | 0.5 | 0.30 | only with another source |
| act on no fetched page | not stored | | |

Each claim's `extra` records the model, the prompt version, the check and, for
`specific`, the quote. A show with only `page` evidence (or none) is asked again
after 90, 180, then 365 days; a done show isn't asked again.

In a trial on Ty Segall's Australian shows (Feb–Mar 2026, 5 shows) the tour
and venue searches (4 credits), 18 page fetches and one Gemini call (~9,000
tokens) found every opener, each with a show-specific quote, plus two dates the
ledger didn't have.

Budgets: a run spends whatever the providers allow, as fast as their
per-minute limits allow; each request is counted (and committed) before it is
sent.

- SearXNG (`usage` source `searxng`): a metasearch server run from source on
  this machine (`deploy/searxng.sh install`; `deploy/run-gemini.sh` starts and
  stops it). It scrapes Google (its "cse" endpoint) and Brave, the only engines
  that gave usable gig results from a home connection (Bing ignores quoted
  phrases; Mojeek and DuckDuckGo refuse; `deploy/searxng-settings.yml`). Free
  and uncapped, but Google flagged "unusual traffic" after ~100 searches a few
  seconds apart in one afternoon and Brave suspends for 3 minutes after a few
  in a row, so searches go 15 s apart, and after 3 searches in a row with no
  results and engines down the run moves to Firecrawl. Scraping search engines
  is against their terms; Firecrawl and Gemini are the sanctioned path.
- Firecrawl (`usage` source `firecrawl`, in credits; a 10-result search costs
  2; the free plan has 1,000 a month): searches 6.5 s apart (free plan: 10 a
  minute). HTTP 402 (out of credits) ends the run. Optional caps:
  `FIRECRAWL_MONTHLY_CREDITS` a calendar month, `--max-credits` a run.
- Gemini (`usage` source `gemini`, Pacific day): free tier for
  `gemini-3.5-flash-lite` is 15 requests/min, 500/day, 250k tokens/min; calls
  go 4.5 s apart, at most 500 a day (`--max-requests` a run), prompts ≤ ~15k
  tokens. A 429 for the day's quota ends the run. (`gemini-3.8-flash` is
  1/min and 20/day on the free tier: too few for this.) 1,000 Firecrawl
  credits need ~300–350 Gemini calls, so a month's credits fit in one day.
- A per-minute 429 from either waits (`Retry-After` / `retryDelay`, else 60 s)
  and retries, 3 times at most; then the run ends. The shows stay due.
  "Overloaded" (HTTP 500/502/503/504) waits 30 s and retries, 3 times at most.
- 1,000 credits take ~2–3 hours (searches, page fetches and Gemini calls).
- `concertdb gemini` is its own process with its own lock
  (`ledger.gemini.lock`), next to `run` and `pollstar`; it commits before every
  request and holds the write lock only while storing (see "Concurrency").
- Search results (`data/raw/searxng/`, `data/raw/firecrawl/`) and Gemini
  answers (`data/raw/gemini/`) are archived; fetched pages are not.

Known cases: `concertdb/gemini_cases.json` lists shows whose bills we know (Ty
Segall's Australian tour, Iguana Death Cult at Zebulon, Osees at Club Soda,
Ferris Wheelers and Le Transbordeur, FYF 2017 as a festival).
`concertdb gemini-eval [--case NAME]` runs the step on them for real in a
throwaway database (`data/eval/`, ~10 Gemini calls) and scores what it found;
`tests/test_gemini.py` replays the Ty Segall case offline from captured search
answers and the promoter's tour page.

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
4. **More from the Gemini step**: a second search provider next to Firecrawl
   (1,000 credits a month is ~15 tours a day at best); fairness across artists
   (a cap on tours per artist per run); Pollstar's `promoters` field as a search
   term; a second model to confirm roles on the fetched pages, cached by
   (page hash, prompt version), with a hard monthly $ cap from `usage`.
5. **Bandsintown** past events, if an `app_id` is granted.
6. **MusicSceneMap import**: load `opened_for.csv` into the graph build. The
   ledger's saved Pollstar events could also serve MusicSceneMap's concert
   recommendations, so it calls Pollstar less.
