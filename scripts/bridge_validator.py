#!/usr/bin/env python3
"""Hermes-aware bridge validator: reviews notifications before delivery.

Rules (implement Hermes trading logic as hard filters):
  1. Never sell a winner: skip reduce/sell if pnl > +3%
  2. Never sell a small loser: skip reduce/sell if pnl -5%~+3% (noise)
  3. Never buy a deep loser: skip buy if pnl < -20%
  4. Contradictory batch: skip buy if sell exists for same stock
  5. Zero-hold sell: skip sell if holding 0 shares
  6. Briefing/review: always pass through (no stock code or no action)
"""

import json
import re
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

REPO = Path("/root/projects/stock-advisor-bot")
SNAPSHOT_PATH = REPO / "portfolio-snapshot.json"
SKIPPED_LOG = REPO / "data" / "bridge_skipped.log"


def load_holdings() -> dict[str, dict]:
    """Load current positions: code -> {quantity, cost_price, current_price, pnl_pct}."""
    holdings: dict[str, dict] = {}
    if not SNAPSHOT_PATH.exists():
        return holdings
    try:
        data = json.loads(SNAPSHOT_PATH.read_text())
        for h in data.get("holdings", []):
            code = h.get("code", "")
            qty = h.get("quantity", 0)
            cost = Decimal(str(h.get("costPrice", 0)))
            price = Decimal(str(h.get("currentPrice", 0)))
            pnl = float((price - cost) / cost * 100) if cost > 0 else 0
            holdings[code] = {
                "quantity": qty,
                "cost_price": float(cost),
                "current_price": float(price),
                "pnl_pct": pnl,
            }
    except Exception:
        pass
    return holdings


def validate_message(msg: str, holdings: dict[str, dict]) -> bool:
    """Return True if message should be delivered, False to skip."""
    msg = msg.strip()
    if not msg or len(msg) < 10:
        return False

    code_match = re.search(r'\b(\d{6})\b', msg)
    code = code_match.group(1) if code_match else None

    action = None
    if '买入' in msg or 'buy' in msg.lower():
        action = 'buy'
    elif re.search(r'卖出|清仓|减仓|reduce|avoid|sell', msg):
        action = 'sell'

    # Pass-through: messages without stock code or action
    if not code or not action:
        return True

    holding = holdings.get(code)
    if not holding:
        return True  # New stock → allow

    qty = holding["quantity"]
    pnl = holding["pnl_pct"]

    # ── RULE 1: Never sell a winner (>+3%) ──
    if action == 'sell' and pnl > 3.0:
        return False

    # ── RULE 2: Never sell a small loser (-5% to +3%) ──
    if action == 'sell' and pnl > -5.0 and pnl <= 3.0:
        return False

    # ── RULE 3: Never buy a deep loser (<-20%) ──
    if action == 'buy' and pnl < -20.0:
        return False

    # ── RULE 5: Zero-hold sell ──
    if action == 'sell' and qty <= 0:
        return False

    return True


def main():
    text = sys.stdin.read()
    if not text.strip():
        sys.exit(0)

    messages = re.split(r'\n={10,}\n|\n---\n|\n{3,}', text.strip())

    holdings = load_holdings()
    valid = []
    skipped = []
    positions_seen: dict[str, set] = {}

    for msg in messages:
        msg = msg.strip()
        if not msg or len(msg) < 10:
            continue

        code_match = re.search(r'\b(\d{6})\b', msg)
        code = code_match.group(1) if code_match else None

        action = None
        if '买入' in msg or 'buy' in msg.lower():
            action = 'buy'
        elif re.search(r'卖出|清仓|减仓|reduce|avoid|sell', msg):
            action = 'sell'

        # RULE 4: Contradictory batch
        if code and action:
            if code not in positions_seen:
                positions_seen[code] = set()
            if action == 'buy' and 'sell' in positions_seen[code]:
                skipped.append(f"SKIP {code}: contradictory buy after sell")
                continue
            if action == 'sell' and 'buy' in positions_seen[code]:
                valid = [m for m in valid if code not in m]
            positions_seen[code].add(action)

        if not validate_message(msg, holdings):
            title_match = re.search(r'【(.+?)】', msg)
            title = title_match.group(1) if title_match else "unknown"
            pnl = holdings.get(code, {}).get('pnl_pct', 0) if code else 0
            skipped.append(f"SKIP {code}: {title} (pnl={pnl:.1f}%)")
            continue

        valid.append(msg)

    if skipped:
        SKIPPED_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(SKIPPED_LOG, "a") as f:
            f.write(f"\n[{datetime.now().isoformat()}] Skipped {len(skipped)}:\n")
            for s in skipped:
                f.write(f"  {s}\n")

    if not valid:
        sys.exit(0)

    for msg in valid:
        print(msg)
        print('---')


if __name__ == "__main__":
    main()
