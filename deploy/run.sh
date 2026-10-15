#!/usr/bin/env bash
# One scheduled cycle. Loads secrets from .env (never committed), appends to
# data/run.log. Used by Windows Task Scheduler (via WSL) and works from cron.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
mkdir -p data
{ echo "=== $(date -u +%FT%TZ)"; .venv/bin/concertdb run; } >> data/run.log 2>&1
