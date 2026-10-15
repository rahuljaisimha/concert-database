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
| Pollstar | Upcoming shows with supporting acts (same endpoints MusicSceneMap uses) | planned |
| Wayback Machine | Archived venue calendars for past years | planned |
| Web search + Claude | Fills specific gaps (a known show with no opener listed) | planned |
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

# Load every artist from MusicSceneMap's graph (~76k, a few seconds)
.venv/bin/concertdb seed --graph ../MusicSceneMap/public/graph.db.gz

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

# One full cycle: watched pages, the day's setlist.fm budget, backup
concertdb run

concertdb status                      # coverage and progress
concertdb shows "Flat Worms"          # what we know about an artist
concertdb export --graph-only         # data/export/opened_for.csv for MusicSceneMap
```

On the always-on machine, `deploy/concertdb.timer` runs `concertdb run` once a
day (and catches up after the machine was off).

## Backups

`concertdb run` ends with a backup: a consistent snapshot of the database plus
the raw response archive, uploaded to a Cloudflare R2 bucket when the `R2_*`
variables are set (7 daily + 8 weekly snapshots kept). On a new machine,
`concertdb restore` downloads the newest snapshot and the archive, and the
crawl continues where it stopped.

## Tests

```bash
.venv/bin/pytest
```

Offline and fast. Fixtures are in `tests/fixtures/` (see its README).
