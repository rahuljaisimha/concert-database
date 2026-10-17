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
| Gemini + Google Search | Fills specific gaps (a known show with no opener listed); answers are checked against the pages they cite | ✅ `concertdb gemini` |
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
# downloads graph.db.gz from GitHub, or pass --graph <path>)
.venv/bin/concertdb seed

# Crawl the artists you care about first
.venv/bin/concertdb prioritize "Osees" "Flat Worms" "Ty Segall"
```

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

# Ask Gemini for openers of past shows that lack one (up to 1,400 a day, the
# free tier's search quota; can run while `concertdb run` is running)
concertdb gemini

concertdb status                      # coverage and progress
concertdb shows "Flat Worms"          # what we know about an artist
concertdb export --graph-only         # data/export/opened_for.csv for MusicSceneMap
```

On the always-on machine, two daily scheduled jobs start at 4 am and catch up
after the machine was off: `deploy/run.sh` (one `concertdb run`, logged to
`data/run.log`) and `deploy/run-pollstar.sh` (the long Pollstar process,
logged to `data/pollstar.log`). Windows Task Scheduler via WSL
(`deploy/windows-task.ps1`) or systemd timers on Linux (`deploy/*.timer`).
A third daily task runs `deploy/gemini.sh` (`concertdb gemini`, logged to
`data/gemini.log`).
See docs/HOSTING.md.

## Backups

`concertdb run` ends with a backup: a consistent snapshot of the database plus
the raw response archive, uploaded to a Cloudflare R2 bucket when the `R2_*`
variables are set (7 daily + 8 weekly snapshots kept). On a new machine,
`concertdb restore` downloads the newest snapshot and the archive, and the
crawl continues where it stopped.

## Tests

```bash
.venv/bin/pytest                 # every change: offline, under a second
.venv/bin/concertdb smoke-test   # after changing a source: live, ~10 requests
```

`pytest` uses fixtures in `tests/fixtures/` (see its README). `smoke-test`
makes a few real requests per source (setlist.fm for Flat Worms, Pollstar for
Zebulon, one search-grounded Gemini request for the Iguana Death Cult show at
Zebulon, optionally one page with `--page-url`) into a throwaway database
under `data/smoke/`, prints PASS/FAIL per source, and never touches the real
ledger or its daily budgets. Its saved responses in `data/smoke/raw/` make
good new test fixtures.
