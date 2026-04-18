#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$BASE_DIR/run/stock-advisor.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "stock-advisor is not running"
  exit 0
fi

PID="$(cat "$PID_FILE")"
if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "stopped stock-advisor: pid=$PID"
else
  echo "stale pid file removed"
fi

rm -f "$PID_FILE"
