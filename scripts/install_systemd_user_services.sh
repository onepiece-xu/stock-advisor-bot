#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_SRC_DIR="$BASE_DIR/systemd/user"
UNIT_DST_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$UNIT_DST_DIR"

cp "$UNIT_SRC_DIR/stock-advisor-monitor.service" "$UNIT_DST_DIR/"
cp "$UNIT_SRC_DIR/stock-advisor-feishu-bot.service" "$UNIT_DST_DIR/"

systemctl --user daemon-reload

echo "Installed user services into $UNIT_DST_DIR"
echo
echo "Next steps:"
echo "  1. python3 -m stock_advisor.cli validate-config --config $BASE_DIR/config.yaml"
echo "  2. systemctl --user enable --now stock-advisor-monitor.service"
echo "  3. systemctl --user enable --now stock-advisor-feishu-bot.service"
echo
echo "Useful commands:"
echo "  systemctl --user status stock-advisor-monitor.service"
echo "  systemctl --user status stock-advisor-feishu-bot.service"
echo "  journalctl --user -u stock-advisor-monitor.service -f"
echo "  journalctl --user -u stock-advisor-feishu-bot.service -f"
