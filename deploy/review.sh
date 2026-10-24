#!/usr/bin/env bash
# Review the act names the Gemini step flagged (see concertdb/review.py):
#   deploy/review.sh                         list them, with their shows and pages
#   deploy/review.sh keep "NAME"             the name is right
#   deploy/review.sh rename "NAME" "NEW"     store it as NEW
#   deploy/review.sh mbid "NAME" MBID        it is this MusicBrainz artist
#   deploy/review.sh delete "NAME"           not an act
# Loads .env so the dashboard on your phone is re-uploaded after each change.
# Works from any directory; the dashboard's "Names to review" shows each command.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi
exec .venv/bin/concertdb review "$@"
