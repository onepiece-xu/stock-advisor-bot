#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$BASE_DIR/logs"
PID_DIR="$BASE_DIR/run"
PID_FILE="$PID_DIR/stock-advisor.pid"
LOG_FILE="$LOG_DIR/monitor.log"
CONFIG_FILE="${1:-$BASE_DIR/config.yaml}"

mkdir -p "$LOG_DIR" "$PID_DIR"

if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE")"
  if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
    echo "stock-advisor already running: pid=$PID"
    exit 0
  else
    rm -f "$PID_FILE"
  fi
fi

cd "$BASE_DIR"
nohup python3 -m stock_advisor.cli monitor-daemon --config "$CONFIG_FILE" >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "started stock-advisor: pid=$(cat "$PID_FILE") log=$LOG_FILE"
