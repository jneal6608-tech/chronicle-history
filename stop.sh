#!/usr/bin/env bash
# Stops the Chronicle Shorts engine. A job that was mid-way can be resumed next time.
cd "$(dirname "$(readlink -f "$0")")"
if [ -f data/engine.pid ] && kill "$(cat data/engine.pid)" 2>/dev/null; then
  rm -f data/engine.pid; echo "Chronicle Shorts stopped."
elif pkill -f "$(pwd)/.venv/bin/python -m app" 2>/dev/null || pkill -f ".venv/bin/python -m app --no-browser" 2>/dev/null; then
  echo "Chronicle Shorts stopped."
else
  echo "Chronicle Shorts wasn't running."
fi
