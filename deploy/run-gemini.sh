#!/usr/bin/env bash
# The Gemini step as its own process (~1,400 search-grounded requests, a few
# hours), started daily next to deploy/run.sh and deploy/run-pollstar.sh.
# Only one runs at a time (ledger.gemini.lock). Appends to data/gemini.log.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
export PYTHONUNBUFFERED=1
mkdir -p data
{ echo "=== $(date -u +%FT%TZ)"; .venv/bin/concertdb gemini; } >> data/gemini.log 2>&1
