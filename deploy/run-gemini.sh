#!/usr/bin/env bash
# The search + Gemini step as its own process (up to a few hours), started next
# to deploy/run.sh and deploy/run-pollstar.sh. Only one runs at a time
# (ledger.gemini.lock). Appends to data/gemini.log.
# With SEARXNG_URL set, starts the local SearXNG first (deploy/searxng.sh) and
# stops it afterwards if this script started it.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
export PYTHONUNBUFFERED=1
mkdir -p data
{
  echo "=== $(date -u +%FT%TZ)"
  started=""
  if [ -n "${SEARXNG_URL:-}" ] && ! deploy/searxng.sh status >/dev/null 2>&1; then
    deploy/searxng.sh start && started=1 || echo "searxng didn't start; Firecrawl only"
  fi
  .venv/bin/concertdb gemini || true
  [ -z "$started" ] || deploy/searxng.sh stop
} >> data/gemini.log 2>&1
