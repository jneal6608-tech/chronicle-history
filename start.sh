#!/usr/bin/env bash
# Starts the Chronicle Shorts engine in the background (if it isn't running) and opens the studio in Chrome.
cd "$(dirname "$(readlink -f "$0")")"
[ -x .venv/bin/python ] || { echo "Chronicle Shorts isn't installed yet. Run: bash install.sh"; exit 1; }
mkdir -p data
PORT=$(.venv/bin/python -c "from app import config; print(config.PORT)")
URL=$(.venv/bin/python -c "from app import config; print(config.studio_urls()[0])")
ALT=$(.venv/bin/python -c "from app import config; u=config.studio_urls(); print(u[1] if len(u)>1 else '')")
healthy() { curl -s -m 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; }

if ! healthy; then
  echo "Starting the Chronicle Shorts engine…"
  nohup .venv/bin/python -m app --no-browser >> data/engine.log 2>&1 &
  echo $! > data/engine.pid
  for _ in $(seq 1 60); do healthy && break; sleep 0.5; done
  if ! healthy; then
    echo "The engine did not start. Last lines of data/engine.log:"
    tail -n 25 data/engine.log
    exit 1
  fi
fi
echo "Chronicle Shorts is running:  $URL"
[ -n "$ALT" ] && echo "If that page doesn't load:    $ALT"
if command -v garcon-url-handler >/dev/null; then garcon-url-handler "$URL" >/dev/null 2>&1 &
elif command -v xdg-open >/dev/null; then xdg-open "$URL" >/dev/null 2>&1 &
fi
exit 0
