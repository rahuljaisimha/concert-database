# concert-database

A ledger of concert bills: who played which show, and who opened for whom.
It feeds `opened_for` edges into [MusicSceneMap](https://github.com/rahuljaisimha/MusicSceneMap),
where they connect bands that share members or scenes (Flat Worms opening for
Iguana Death Cult at Zebulon links them to Osees through Flat Worms' bassist).

No single source has this data, so the ledger collects bills from several
sources, merges them into one record per show, and keeps where every fact
came from:

| Source | What it gives | Status |
|---|---|---|
| Manual entry | Shows you went to. Most trustworthy. | ✅ `concertdb add-show` |
| setlist.fm API | Past shows by MusicBrainz ID; bills inferred when several acts have setlists for the same night | ✅ `concertdb crawl-setlistfm` |
| schema.org event data | Venue calendars and ticketing pages (DICE, Eventbrite, Tixr, venue sites) with full bills | ✅ `concertdb capture-add` / `capture` |
| Pollstar | Upcoming shows with headliner/support flags, saved before they disappear (past dates aren't public) | ✅ `concertdb pollstar-add` / `pollstar` |
| Concert Archives (concertarchives.org) | Fan-logged past shows with full lineups, often older and smaller shows | planned (checking terms first) |
| Wayback Machine | Archived venue calendars for past years | planned |
| Web search + Gemini | Fills specific gaps (a known show with no opener listed), a tour at a time: Firecrawl search finds pages, Gemini reads them, answers are checked against the pages; also adds tour dates the ledger lacks | ✅ `concertdb gemini` |
| Bandsintown | Past events with lineups (needs an `app_id`) | planned |

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how it works and
[docs/HOSTING.md](docs/HOSTING.md) for where to run it.

## Setup

```bash
git clone https://github.com/rahuljaisimha/concert-database && cd concert-database
python3 -m venv .venv
.venv/bin/pip install -e '.[r2,dev]'
cp .env.example .env        # add your setlist.fm key and R2 credentials
set -a; . ./.env; set +a

# Load every artist from MusicSceneMap's graph (~76k, a few seconds;
# downloads graph.db.gz from GitHub, or pass --graph <path>). A one-time seed:
# nothing reads the graph afterwards.
.venv/bin/concertdb seed

# MusicBrainz's artist names, for matching names from Pollstar, Gemini and pages
# (see "MusicBrainz data" below; ~1.5 min, ~0.8 GB in data/musicbrainz.sqlite)
.venv/bin/concertdb mb-load

# Crawl the artists you care about first
.venv/bin/concertdb prioritize "Osees" "Flat Worms" "Ty Segall"
```

## MusicBrainz data

`data/musicbrainz.sqlite` is a lookup copy of MusicBrainz's artists (3M
artists, 3.5M names and aliases, each artist's disambiguation note). It is
used only to match names to MusicBrainz IDs; the artists that appear in our
data are copied into the ledger. It isn't backed up: build it on any computer
with

```bash
.venv/bin/concertdb mb-load                 # newest full export, streamed
.venv/bin/concertdb mb-load --dump PATH     # or a downloaded mbdump.tar.bz2
```

- Source: MusicBrainz's full data export,
  https://data.metabrainz.org/pub/musicbrainz/data/fullexport/ (`LATEST` names
  the newest folder; the file is `mbdump.tar.bz2`, ~7 GB, refreshed twice a
  week). MusicBrainz's core data is public domain (CC0).
- Nothing big is downloaded to disk: the file is read as a stream, and the two
  tables needed (`artist`, `artist_alias`) come first in it, so ~500 MB is
  read and the download stops. Needs network access to data.metabrainz.org,
  ~1 GB free disk, a few hundred MB of memory.
- The result is written to `musicbrainz.sqlite.tmp`, then renamed, so a
  failed run leaves the old file in place. Every connection to the ledger
  attaches it as `mb` if it exists; without it, names only match artists
  already in the ledger.
- Re-run it now and then (monthly is plenty) to pick up new artists and
  aliases. After `concertdb restore` on a new machine, run it once.

## Everyday use

```bash
# A show you went to
concertdb add-show --date 2026-09-17 --venue Zebulon --city "Los Angeles" \
    --headliner "Iguana Death Cult" --opener "Flat Worms"

# Watch a venue calendar or ticketing page (rechecked automatically)
concertdb capture-add https://dice.fm/venue/zebulon-...

# Save a venue's upcoming Pollstar shows (venues already in the ledger are
# looked up automatically; this is for adding one by hand)
concertdb pollstar-add Zebulon --city "Los Angeles"

# One full cycle: watched pages, the day's setlist.fm budget, a little
# Pollstar, backup
concertdb run

# Pollstar on its own: ~1 request/s until the day's budget (20,000 by default)
# or the due work runs out. Runs next to `run`; `run` then skips Pollstar.
concertdb pollstar

# Find openers of past shows with only one act listed: web search (context.dev,
# a local SearXNG via deploy/searxng.sh, then Firecrawl) + Gemini, a tour at a time
# (needs GEMINI_API_KEY and one of CONTEXT_DEV_API_KEY, SEARXNG_URL, FIRECRAWL_API_KEY; stops at a quota;
# can run while `concertdb run` is running; GEMINI_MAX_ACTS / --max-acts widens it)
concertdb gemini
concertdb gemini-eval                 # the same, live, on two shows whose bills we know (--all: every case)

concertdb mb-load                     # MusicBrainz names/aliases for matching (now and then; a few minutes)
concertdb status                      # coverage and progress
concertdb dashboard                   # data/dashboard.html (also written after every scheduled step)
concertdb shows "Flat Worms"          # what we know about an artist
concertdb export --musicbrainz-only   # data/export/opened_for.csv for MusicSceneMap
```

On the always-on machine, three daily scheduled jobs run side by side and
catch up after the machine was off: `deploy/run.sh` (one `concertdb run`,
logged to `data/run.log`) and `deploy/run-pollstar.sh` (the long Pollstar
process, logged to `data/pollstar.log`) at 4 am, and `deploy/run-gemini.sh`
(`concertdb gemini`, logged to `data/gemini.log`) at 1 am, soon after
Gemini's free quota resets at midnight Pacific. Windows Task Scheduler via
WSL (`deploy/windows-task.ps1`) or systemd timers on Linux (`deploy/*.timer`).
See docs/HOSTING.md.

## Dashboard

`data/dashboard.html` is one self-contained page (open it in a browser; no
server, no network) rewritten at the end of every `run`, `gemini` and
`pollstar` step, or on demand with `concertdb dashboard`. Set
`CONCERTDB_DASHBOARD` to also write a copy somewhere handier (on WSL, e.g.
`/mnt/c/Users/<you>/concertdb-dashboard.html`). With the `R2_*` variables set
it is also uploaded to the backup bucket as `dashboards/index.html`, which a
small Worker behind Cloudflare Access serves to your phone (docs/HOSTING.md,
"Dashboard on your phone").

- **The data**: concerts (past, upcoming, cancelled), artists with concerts
  and how many are matched to MusicBrainz, acts per concert (0 / 1 / 2 / 3 /
  4+), concerts with 2+ acts, opened-for pairs (what `export
  --musicbrainz-only` gives MusicSceneMap), venues, cities and countries,
  concerts by year, new concerts per day, top countries, what each source
  contributes, flagged act names to review. Headline numbers show the change
  over 24 hours and 7 days, and their growth over time.
- **The runs**: the last run of each step (when, how long, finished or not,
  what it did), requests per day per source against their limits, the Gemini
  step's outcomes per run, context.dev credits left and when they run out,
  new setlists per run, Pollstar's pace and pause, the last good backup, work
  queues, and the last 40 steps.
- **Alerts** at the top: no backup in 26 hours, a failed or killed step,
  Pollstar paused, context.dev credits running out within two weeks, names to
  review.

Each step is recorded in the `runs` table, and the headline numbers in
`metric_snapshots` each time the page is written (see docs/ARCHITECTURE.md).

## Backups

`concertdb run` ends with a backup: a consistent snapshot of the database plus
the raw response archive, uploaded to a Cloudflare R2 bucket when the `R2_*`
variables are set (7 daily + 8 weekly snapshots kept). The raw archive is kept
for 30 days, locally and in R2 (see "Raw archive" in docs/ARCHITECTURE.md).
On a new machine,
`concertdb restore` downloads the newest snapshot and the archive, and the
crawl continues where it stopped.

## Tests

```bash
.venv/bin/pytest                 # every change: offline, under a second
.venv/bin/concertdb smoke-test   # after changing a source: live, ~10 requests
```

`pytest` uses fixtures in `tests/fixtures/` (see its README). `smoke-test`
makes a few real requests per source (setlist.fm for Flat Worms, Pollstar for
Zebulon, one search and one Gemini call for the Iguana Death Cult show at
Zebulon, optionally one page with `--page-url`) into a throwaway database
under `data/smoke/`, prints PASS/FAIL per source, and never touches the real
ledger or its daily budgets. Its saved responses in `data/smoke/raw/` make
good new test fixtures.
