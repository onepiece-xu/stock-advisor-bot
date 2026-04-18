#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$BASE_DIR/run/stock-advisor.pid"
LOG_FILE="$BASE_DIR/logs/monitor.log"

if [[ ! -f "$PID_FILE" ]]; then
  echo "stock-advisor is not running"
  exit 0
fi

PID="$(cat "$PID_FILE")"
if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
  echo "stock-advisor is running: pid=$PID"
  echo "log: $LOG_FILE"
else
  echo "stock-advisor pid file exists but process is not running"
fi
