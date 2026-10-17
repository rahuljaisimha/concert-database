#!/usr/bin/env bash
# The Pollstar crawler as its own long process (hours at ~1 request/second),
# started daily next to deploy/run.sh. Appends to data/pollstar.log. Only one
# runs at a time; `concertdb run` skips Pollstar while this one is running.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
export PYTHONUNBUFFERED=1   # write log lines as they happen, not at the end
mkdir -p data
{ echo "=== $(date -u +%FT%TZ)"; .venv/bin/concertdb pollstar; } >> data/pollstar.log 2>&1
