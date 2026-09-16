#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PID_FILE="data/paper_realistic.pid"
LOG_FILE="data/paper_realistic.log"
DB_FILE="data/paper_realistic.db"
BREAKERS_FILE="data/paper_realistic_breakers.json"

if [[ -f "$PID_FILE" ]]; then
  old_pid="$(cat "$PID_FILE" || true)"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "realistic paper already running: pid=$old_pid"
    exit 0
  fi
fi

mkdir -p data
nohup env \
  PAPER_TRADING=true \
  REALISTIC_PAPER_EXECUTION=true \
  ENABLE_WEATHER_TRADER=true \
  ENABLE_THRESHOLD_TRADER=true \
  DB_PATH="$DB_FILE" \
  BREAKERS_STATE_FILE="$BREAKERS_FILE" \
  LOG_LEVEL=INFO \
  .venv/bin/python -m bot.app > "$LOG_FILE" 2>&1 &

pid="$!"
echo "$pid" > "$PID_FILE"
echo "started realistic paper: pid=$pid db=$DB_FILE log=$LOG_FILE"
