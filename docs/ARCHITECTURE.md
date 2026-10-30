# Architecture

## Data model (`concertdb/db.py`)

```
artists   id = MusicBrainz ID, or 'name:<normalized>' when not matched to MusicBrainz;
          + MusicBrainz's disambiguation note
venues    + venue_source_ids (each source's own venue id → our venue)
shows     one per venue per night (see "Which records are the same show");
          + sources: every source that has looked at the show, in order (JSON
          array). Cumulative: a claim adds its source, a source that found
          nothing adds itself (Gemini), a record moving shows brings its
          show's sources along, and nothing is ever taken off
claims    what ONE source says about ONE artist at ONE show:
          role (headliner/opener/unknown), position, confidence, url, extra JSON
billings  the resolved bill, recomputed from all claims for a show
crawl_state  one row per unit of work: next_due, adaptive interval, cursor, etag, hash
usage     requests and $ per source per day (budgets, caps; Gemini's day is
          Pacific time, when its free quota resets)
mb.*      data/musicbrainz.sqlite, attached: every MusicBrainz artist and its
          names (see "Matching names"); derived, not backed up
```

### Matching names

setlist.fm sends MusicBrainz IDs; Pollstar, Gemini and pages send only names.
Names are matched against MusicBrainz itself, not the MusicSceneMap graph:
the graph is only the one-time seed of the artist list (`concertdb seed`), and
it is built from this ledger's export, so matching against it would be
circular.

`concertdb mb-load` builds `data/musicbrainz.sqlite` from MusicBrainz's full
data export (`mbdump.tar.bz2`, ~7 GB, read as a stream: the `artist` and
`artist_alias` tables come first, so it reads ~500 MB and stops): every
artist's MBID, name, disambiguation note ("US garage rock band") and type,
and each artist's name and aliases, normalized. Sort names ("Cooper, Alan")
are left out: they collide with other artists' real names. It sits next to
the ledger, is attached to every connection as `mb`, and isn't backed up
(rebuilt in a few minutes; run it again now and then to refresh).

A name with no MBID resolves to:
1. the one MusicBrainz artist with that name or alias ("Thee Oh Sees" →
   Osees), added to the ledger with its MusicBrainz name and disambiguation
   note if it isn't there yet (not queued for setlist.fm: that's
   `concertdb prioritize`);
2. if several MusicBrainz artists have it ("Camille", "Nirvana"), the one
   already in the ledger, if exactly one is;
3. otherwise an unmatched `name:` artist, so the fact is still recorded.
   Telling homonyms apart by where the show was (MusicBrainz's area) is a
   possible next step.

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
  get their full history at once, and so do the artists MusicSceneMap users
  looked for and didn't find (see "Search misses from MusicSceneMap").
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

A timeout or dropped connection (no HTTP answer) counts as a request, waits
15 s, 60 s, then 180 s and retries the same page. A fourth in a row ends the
run cleanly (`stopped` = "setlist.fm not answering (…)" in the run's stats,
the step still recorded as finished); the cursor resumes from that page.

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

## Runs and the dashboard (`concertdb/runs.py`, `concertdb/dashboard.py`)

Every scheduled step (`capture`, `setlistfm`, `pollstar`, `gemini`, `backup`)
goes through `runs.record()`: a `runs` row is written when it starts and
completed with `ok` and the step's counts (JSON) when it ends, or with the
error if it raised. A row without `finished_at` is a step still running or one
whose process was killed. The Gemini step's counts include context.dev's
`credits_remaining` from the last search (`contextdev_balance`).

`dashboard.collect()` reads the ledger (concerts, artists, lineups),
`runs`, `usage` (requests per day) and `crawl_state` (queues), and stores the
headline numbers in `metric_snapshots` for trends and 24 h / 7 day deltas.
`dashboard.write()` renders `concertdb/dashboard_template.html` with that data
embedded as JSON; the page draws its own charts (inline SVG, no libraries).
A dashboard error is printed but never fails a run.

## Search misses from MusicSceneMap (`concertdb/misses.py`)

MusicSceneMap's graph is built from the artists with concerts here, so an
artist its users look for and can't find gets in by being crawled. MusicSceneMap
logs each such search in its Supabase table `search_misses` (`id, query,
source, created_at`): the MusicBrainz ID when the user picked the artist from
its dropdown's MusicBrainz results, else the typed name.

The first step of every `run` (or `concertdb misses`) reads the misses logged
since the last run with `MSM_MISSES_DB_URL`, a Postgres login that can only
read that table (MusicSceneMap's migration 007), and:

- resolves each query to a MusicBrainz ID: an MBID as is (if MusicBrainz knows
  it); a name through every artist's names and aliases in
  `data/musicbrainz.sqlite` ("Thee Oh Sees" → Osees). One artist with that name
  → it; several → skipped as ambiguous; none → skipped.
- adds the artist to the ledger if needed and queues it for setlist.fm at the
  same priority as `concertdb prioritize` (full history, first in line).
- stops at 25 new artists a run (`MAX_PER_RUN`), so the rest of the queue keeps
  most of the day's 1,400 requests; later misses wait for the next run. Its
  position in the table is `meta.misses_last_id`.

Nothing is written back to MusicSceneMap. A failure (Supabase unreachable) is
recorded as a failed `misses` step and the run carries on.

## User-requested data from MusicSceneMap (`concertdb/msm_requested.py`)

MusicSceneMap fetches concert data from outside sources because a user asked
for something, and keeps it in its Supabase table `user_requested_data`
(`source, key, data, version, fetched_at, seq`; MusicSceneMap migration 009),
one row per (source, key); `seq` moves forward when a row is added or its
version changes. Nothing in it is about the user. Sources so far:

- `setlistfm` (key: setlist id): every setlist MusicSceneMap's venue-search
  function fetches (an artist's setlists in a city, then each venue's), as
  setlist.fm returned it.
- `pollstar` (key: Pollstar venue id): a venue (id, name, city, state,
  location, from Pollstar's venue search) and Pollstar's response for its
  upcoming events, fetched by MusicSceneMap's pollstar-events function for its
  Discover page (Pollstar is asked server-side, at most once a day per venue,
  and every visitor is served from the cache).
- `pollstar_city` (key: `<city id>:<page>` or `<city id>:<from>:<to>:<page>`):
  one page (up to 100 events) of Pollstar's events in a city between two dates,
  each event with its full venue (address, city, country, coordinates),
  fetched by MusicSceneMap's pollstar-city function, which replaced
  pollstar-events for Discover's concert search (same once-a-day cache).
- `pollstar_search` (key: a venue or city search): those functions' cache of
  Pollstar's autocomplete answers; skipped here.

The second step of every `run` (or `concertdb msm-requested`) reads the rows
past `meta.msm_requested_last_seq` (at most 5,000) with the same login as the
misses and hands each to its source's ingest (`INGEST`), which uses that
source's crawl code, so it's stored as if we had fetched it, without spending
our requests:

- `setlistfm`: `setlistfm.parse_setlist` / `is_known` / `ingest_setlist`
  (source `setlistfm`, the setlist id as native_id, the bill re-inferred, the
  venue queued for a venue crawl); unchanged setlists are skipped.
- `pollstar`: the venue linked like `pollstar-add` (a new one is created and
  queued for our own Pollstar crawl), its events through
  `pollstar.parse_events` / `ingest_event` with `extra.via = "musicscenemap"`
  (where they came from).
- `pollstar_city`: the events grouped by venue; each venue is found by its
  Pollstar id, or created with the page's city, state, country and coordinates
  (taken from its physical address) and linked, but not queued for our own
  Pollstar crawl, since the city pages MusicSceneMap's users ask for already
  cover it. Then its events are stored like `pollstar`. Events without a venue
  are skipped (counted).

A row from a source without an ingest, or an unreadable one, is skipped; a
failure is recorded as a failed `msm_requested` step and the run carries on.
A new source: an ingest function in `INGEST`.

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
  30 s, 60 s, 120 s, 300 s) and retries; 300 clean requests in a row
  take 20% off the interval, down to the floor
- the process ends as soon as a wait would be longer than 5 minutes
  (`MAX_WAIT_SECONDS`): a fifth pushback in a row, or a `Retry-After` over
  300 s. The next scheduled run tries again, starting at the slower pace this
  one reached
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
so the search is done here and Gemini (`gemini-3.5-flash-lite`, then
`gemini-3.1-flash-lite`; no tools) only reads text we fetched. Search goes, in `SEARCH_ORDER` (default
`contextdev,searxng`), to context.dev's search API and a local SearXNG, each
while it answers, then to Firecrawl's search API.
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
7. Odd act names are kept, never dropped (a wrong name can be recategorised
   later; a dropped act is lost). A name cut short on the page ("The Blind
   Shak...") becomes the one full name on the fetched pages that starts with
   it (`extra.name_check` = `completed`, the original in
   `extra.name_as_given`), or is kept as written with `name_check` =
   `truncated` if there is none or more than one. A name that reads like a
   description rather than an act ("groups from Bay Area Rock Camp for
   Girls", "special guests", 7+ words) is kept with `name_check` =
   `description`, unless it matches a MusicBrainz artist. Both stay `name:`
   artists, so `export --musicbrainz-only` leaves them out. The prompt also
   asks for act names only, in full.
   `concertdb review` (`deploy/review.sh`, linked from the dashboard's alert)
   settles each flagged name: `keep` (`name_check` becomes `kept`), `rename`
   or `mbid` (every claim of the flagged artist moves to the right artist,
   `name_check` becomes `renamed`, the page's name stays in `name_as_given`,
   and the unused `name:` artist goes), or `delete` (its claims go; a show
   left with none goes too). See concertdb/review.py.

| Check | Confidence | × weight 0.6 | Exported at the default 0.4? |
|---|---|---|---|
| act in a show-specific quote found on a page (`specific`) | 0.8 | 0.48 | yes |
| act named on a fetched page that names the show (`page`) | 0.5 | 0.30 | only with another source |
| act on no fetched page | not stored | | |

Each claim's `extra` records the model, the prompt version, the check and, for
`specific`, the quote. What came of looking at a show is kept in its `gemini`
queue entry (`crawl_state`, like any source's unit of work): `last_checked`,
and in its cursor the outcome (`specific`, `page_only`, `festival` or
`not_found`), model and prompt version. Found nothing leaves the show itself as
it was (no claims). A show with only `page` evidence is asked again after 90,
180, then 365 days; any other outcome, including `not_found`, isn't asked again
(many shows have nothing online, so asking again repeats the same empty
searches). An entry Gemini has checked is kept even when another source fills
the show in; only unchecked entries are dropped then. To re-ask shows on
purpose (a better prompt, say), set their `next_due` to now.

In a trial on Ty Segall's Australian shows (Feb–Mar 2026, 5 shows) the tour
and venue searches (4 credits), 18 page fetches and one Gemini call (~9,000
tokens) found every opener, each with a show-specific quote, plus two dates the
ledger didn't have.

Budgets: a run spends whatever the providers allow, as fast as their
per-minute limits allow; each request is counted (and committed) before it is
sent.

- context.dev (`usage` source `contextdev`, in credits): a 10-result search
  costs 1 credit, plus 1 for highlights (the passages relevant to the query),
  so 2 (recorded from `key_metadata.credits_consumed`); full page text would
  be +1 per result, so it isn't asked for. 6.5 s apart (free plan: 10 a
  minute), at most 1,500 credits a run (`CONTEXT_DEV_MAX_CREDITS`; a night's
  run needs ~1,000–1,300), then the next search backend. Pages are still fetched by us; one we can't fetch (Facebook event
  pages) is read from its highlights. The archive keeps the answer without
  page text. Tried on the
  known cases: good for one-show searches (Facebook events "Osees w/ DMBQ",
  "OSEES + OVER-GAIN OPTIMAL DEATH"), weak for the quoted venue-list search.
  Out of credits (HTTP 401/402/403 or `credits_remaining` 0) or 3 failures in
  a row switch it off for the run.
- SearXNG (`usage` source `searxng`): a metasearch server run from source on
  this machine (`deploy/searxng.sh install`; `deploy/run-gemini.sh` starts and
  stops it). It scrapes Google (its "cse" endpoint) and Brave, the only engines
  that gave usable gig results from a home connection (Bing ignores quoted
  phrases; Mojeek and DuckDuckGo refuse; `deploy/searxng-settings.yml`). Free
  and uncapped, but Google flagged "unusual traffic" after ~100 searches a few
  seconds apart in one afternoon and Brave suspends for 3 minutes after a few
  in a row, so searches go 15 s apart, and after 3 searches in a row with no
  results and engines down the run moves to Firecrawl. When nothing else is
  left (Firecrawl out of credits, context.dev at its cap), a failed search is
  asked again after 3 minutes, Brave's suspension, until 3 in a row switch
  SearXNG off and end the run. Scraping search engines
  is against their terms; Firecrawl and Gemini are the sanctioned path.
- Firecrawl (`usage` source `firecrawl`, in credits; a 10-result search costs
  2; the free plan has 1,000 a month): searches 6.5 s apart (free plan: 10 a
  minute). HTTP 402 (out of credits) switches it off for the run: that search
  goes back to context.dev / SearXNG if either is still on (one failed SearXNG
  search no longer ends the run), and the run ends with `stopped` = "no search
  left: …" only when none is. Optional caps:
  `FIRECRAWL_MONTHLY_CREDITS` a calendar month, `--max-credits` a run.
- Gemini (`usage` sources `gemini`, all models, and `gemini:<model>`, Pacific
  day): the free tier's quota is per model; `gemini-3.5-flash-lite` and
  `gemini-3.1-flash-lite` are each 15 requests/min, 500/day, 250k tokens/min.
  A run uses the models in order (`GEMINI_MODELS`; `GEMINI_MODEL` or `--model`
  for just one): when one has made 500 calls today, or Gemini answers 429 for
  its day's quota, the run moves on to the next, and ends when all are spent
  (`--max-requests` caps a run). Calls go 4.5 s apart, prompts ≤ ~15k tokens.
  3.1 found the same openers as 3.5 on the eval cases but answers "overloaded"
  (503) more often; those are waited out like any 503 (below). Claims record
  which model answered (`extra.model`). (`gemini-3.8-flash` is
  1/min and 20/day on the free tier: too few for this.) 1,000 Firecrawl
  credits need ~300–350 Gemini calls, so a month's credits fit in one day.
- A per-minute 429 from either waits (`Retry-After` / `retryDelay`, else 60 s)
  and retries, 3 times at most; then the run ends. The shows stay due.
  "Overloaded" (HTTP 500/502/503/504) waits 30 s and retries, 3 times at most.
- 1,000 credits take ~2–3 hours (searches, page fetches and Gemini calls).
- `concertdb gemini` is its own process with its own lock
  (`ledger.gemini.lock`), next to `run` and `pollstar`; it commits before every
  request and holds the write lock only while storing (see "Concurrency").
- Search results (`data/raw/contextdev/`, `data/raw/searxng/`,
  `data/raw/firecrawl/`) and Gemini answers (`data/raw/gemini/`) are archived;
  page text is not.

Known cases: `concertdb/gemini_cases.json` lists shows whose bills we know (Ty
Segall's Australian tour and its Gold Coast show alone, Iguana Death Cult at
Zebulon, Osees at Club Soda, Ferris Wheelers, Le Transbordeur and the Teragram
billed as Thee Oh Sees with Fuzz, FYF 2017 as a festival).
`concertdb gemini-eval` runs the step for real, in a throwaway database
(`data/eval/`, matching names with `data/musicbrainz.sqlite`), on the two
default cases (Ty Segall at Mo's Desert Clubhouse: Earth Tongue and Speeding
Vehicle; Iguana Death Cult at Zebulon: Flat Worms, not on setlist.fm): ~2
searches, 2 Gemini calls. `--case NAME` or `--all` for the rest (~10 Gemini
calls, ~25 context.dev credits). `tests/test_gemini.py` replays the Ty Segall
tour offline from captured search answers and the promoter's tour page.

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
