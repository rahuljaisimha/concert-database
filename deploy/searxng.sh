#!/usr/bin/env bash
# A local SearXNG for `concertdb gemini` (SEARXNG_URL=http://127.0.0.1:8888).
#   deploy/searxng.sh install   clone SearXNG into ~/searxng-src, a venv in ~/searxng-venv,
#                               settings in ~/searxng/settings.yml (from deploy/searxng-settings.yml)
#   deploy/searxng.sh update    pull the newest SearXNG and reinstall
#   deploy/searxng.sh start     start it in the background unless it is already answering
#   deploy/searxng.sh stop
#   deploy/searxng.sh status
# No Docker: SearXNG runs from source in its own venv. deploy/run-gemini.sh starts
# it before `concertdb gemini` and stops it after.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
SRC="$HOME/searxng-src"; VENV="$HOME/searxng-venv"; CONF="$HOME/searxng"
URL="${SEARXNG_URL:-http://127.0.0.1:8888}"

up() { curl -sf -o /dev/null --max-time 5 "$URL/healthz" || curl -sf -o /dev/null --max-time 5 "$URL/"; }

install() {
  [ -d "$SRC" ] || git clone -q --depth 1 https://github.com/searxng/searxng.git "$SRC"
  [ -d "$VENV" ] || python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -U pip setuptools wheel pyyaml
  "$VENV/bin/pip" install -q -r "$SRC/requirements.txt"
  "$VENV/bin/pip" install -q --no-build-isolation -e "$SRC"
  mkdir -p "$CONF"
  secret="$(grep -o 'secret_key: "[^"]*"' "$CONF/settings.yml" 2>/dev/null | cut -d'"' -f2 || true)"
  [ -n "$secret" ] && [ "$secret" != "@SECRET@" ] || secret="$(head -c 24 /dev/urandom | base64 | tr -d '/+=')"
  sed "s/@SECRET@/$secret/" "$here/searxng-settings.yml" > "$CONF/settings.yml"
  stop >/dev/null || true     # a running instance would keep the old settings
  echo "installed: $(git -C "$SRC" log -1 --format='%h %cd' --date=short)"
}

start() {
  if up; then echo "searxng: already running"; return; fi
  [ -d "$VENV" ] || install
  (cd "$SRC" && SEARXNG_SETTINGS_PATH="$CONF/settings.yml" \
     nohup "$VENV/bin/python" -m searx.webapp >> "$CONF/searxng.log" 2>&1 & echo $! > "$CONF/searxng.pid")
  for _ in $(seq 1 30); do up && { echo "searxng: started"; return; }; sleep 1; done
  echo "searxng: didn't start, see $CONF/searxng.log" >&2; return 1
}

stop() {
  # Every instance from this venv, not just the pid file's (an older one keeps old settings).
  if pkill -f "$VENV/bin/python -m searx.webapp" 2>/dev/null; then echo "searxng: stopped"; fi
  rm -f "$CONF/searxng.pid"
}

case "${1:-status}" in
  install) install ;;
  update) git -C "$SRC" pull -q --ff-only && install ;;
  start) start ;;
  stop) stop ;;
  status) if up; then echo "searxng: up at $URL"; else echo "searxng: down"; exit 1; fi ;;
  *) echo "usage: $0 install|update|start|stop|status" >&2; exit 2 ;;
esac
