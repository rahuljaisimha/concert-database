#!/usr/bin/env bash
# Daily Gemini step: asks for openers of shows that lack one, within the
# free tier's search-grounding quota. Runs alongside deploy/run.sh (it takes
# its own lock). Loads secrets from .env, appends to data/gemini.log.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
export PYTHONUNBUFFERED=1
mkdir -p data
{ echo "=== $(date -u +%FT%TZ)"; .venv/bin/concertdb gemini; } >> data/gemini.log 2>&1
