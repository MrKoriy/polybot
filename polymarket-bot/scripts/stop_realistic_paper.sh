#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PID_FILE="data/paper_realistic.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "realistic paper pid file not found"
  exit 0
fi

pid="$(cat "$PID_FILE" || true)"
if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
  kill "$pid"
  echo "stopped realistic paper: pid=$pid"
else
  echo "realistic paper not running"
fi
rm -f "$PID_FILE"
