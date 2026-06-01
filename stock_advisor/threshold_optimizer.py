#!/usr/bin/env python3
"""Threshold optimizer — grid-search buy_score/hold_score against historical signals.

Usage:
  python3 stock_advisor/threshold_optimizer.py --config config.yaml
  python3 stock_advisor/threshold_optimizer.py --config config.yaml --report
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SIGNAL_LOG = REPO / "data" / "signals" / "signal_log.jsonl"
MARKET_DB = REPO / "data" / "market.db"


def load_signals() -> list[dict]:
    if not SIGNAL_LOG.exists():
        return []
    signals = []
    for line in SIGNAL_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            s = json.loads(line)
            signals.append(s)
        except json.JSONDecodeError:
            continue
    return signals


def get_subsequent_price(symbol: str, signal_time: str, days_later: int = 5) -> Decimal | None:
    """Get price N trading days after signal_time from market.db."""
    if not MARKET_DB.exists():
        return None
    try:
        db = sqlite3.connect(str(MARKET_DB))
        rows = db.execute(
            """SELECT current_price, quote_time FROM quotes
               WHERE symbol=? AND quote_time > ?
               ORDER BY quote_time ASC LIMIT ?""",
            (symbol, signal_time, days_later * 240),
        ).fetchall()
        db.close()

        if not rows:
            return None
        # Take the last available price (closest to N days later)
        return Decimal(str(rows[-1][0]))
    except Exception:
        return None


def backtest_thresholds(
    signals: list[dict],
    buy_score: float,
    hold_score: float,
    reduce_score: float = 38.0,
) -> dict:
    """Simulate trading with given thresholds. Returns stats."""
    # Dedup: max one signal per symbol per 2-hour window
    deduped: dict[str, dict] = {}
    for s in signals:
        ts = s.get("timestamp", "")
        window = ts[:13] if len(ts) >= 13 else ts[:10]  # YYYY-MM-DDTHH
        key = f"{s['symbol']}_{window}"
        if key not in deduped or s.get("score", 0) > deduped[key].get("score", 0):
            deduped[key] = s

    buy_signals = []
    sell_signals = []
    total_signal_count = 0

    for s in deduped.values():
        score = s.get("score", 0)
        if score >= buy_score:
            action = "buy"
        elif score >= hold_score:
            action = "hold"
        elif score <= reduce_score:
            action = "sell"
        else:
            action = "hold"

        total_signal_count += 1
        if action == "buy":
            buy_signals.append(s)
        elif action == "sell":
            sell_signals.append(s)

    # Evaluate buy signals: did price go up after 1 day?
    buy_hits = 0
    for s in buy_signals:
        future_price = get_subsequent_price(s["symbol"], s["timestamp"], days_later=1)
        if future_price and s.get("price", 0) > 0:
            move = float((future_price - Decimal(str(s["price"]))) / Decimal(str(s["price"])) * 100)
            if move > 1.0:
                buy_hits += 1

    # Evaluate sell signals: did price go down after 1 day?
    sell_hits = 0
    for s in sell_signals:
        future_price = get_subsequent_price(s["symbol"], s["timestamp"], days_later=1)
        if future_price and s.get("price", 0) > 0:
            move = float((future_price - Decimal(str(s["price"]))) / Decimal(str(s["price"])) * 100)
            if move < -1.0:
                sell_hits += 1

    return {
        "buy_score": buy_score,
        "hold_score": hold_score,
        "reduce_score": reduce_score,
        "total_signals": total_signal_count,
        "buy_count": len(buy_signals),
        "buy_hits": buy_hits,
        "buy_hit_rate": round(buy_hits / len(buy_signals) * 100, 1) if buy_signals else 0,
        "sell_count": len(sell_signals),
        "sell_hits": sell_hits,
        "sell_hit_rate": round(sell_hits / len(sell_signals) * 100, 1) if sell_signals else 0,
        "overall_hit_rate": round((buy_hits + sell_hits) / max(len(buy_signals) + len(sell_signals), 1) * 100, 1),
    }


def grid_search(signals: list[dict]) -> list[dict]:
    """Grid search buy_score (65-85) and hold_score (50-65)."""
    results = []
    for buy_score in range(65, 88, 3):
        for hold_score in range(50, 68, 2):
            if hold_score > buy_score:
                continue
            r = backtest_thresholds(signals, buy_score, hold_score)
            results.append(r)

    # Sort by overall hit rate
    results.sort(key=lambda x: x["overall_hit_rate"], reverse=True)
    return results


def format_report(results: list[dict], current_buy: float, current_hold: float) -> str:
    if not results:
        return "📊 无足够信号数据进行阈值优化"

    best = results[0]
    current = backtest_thresholds(load_signals(), current_buy, current_hold)

    lines = ["📊 **阈值优化回测报告**"]
    lines.append("")
    lines.append("| 方案 | buy≥ | hold≥ | 总信号 | 买入/命中 | 卖出/命中 | 综合命中 |")
    lines.append("|------|------|-------|--------|-----------|-----------|----------|")

    # Current thresholds
    lines.append(
        f"| 🔵 当前 | {current['buy_score']:.0f} | {current['hold_score']:.0f} | {current['total_signals']} | "
        f"{current['buy_count']}/{current['buy_hits']}({current['buy_hit_rate']}%) | "
        f"{current['sell_count']}/{current['sell_hits']}({current['sell_hit_rate']}%) | "
        f"**{current['overall_hit_rate']}%** |"
    )

    # Top 3 alternatives
    for r in results[:3]:
        emoji = "🟢" if r["overall_hit_rate"] > current["overall_hit_rate"] else "⚪"
        lines.append(
            f"| {emoji} 最优{r['buy_score']:.0f} | {r['buy_score']:.0f} | {r['hold_score']:.0f} | {r['total_signals']} | "
            f"{r['buy_count']}/{r['buy_hits']}({r['buy_hit_rate']}%) | "
            f"{r['sell_count']}/{r['sell_hits']}({r['sell_hit_rate']}%) | "
            f"**{r['overall_hit_rate']}%** |"
        )

    lines.append("")
    if best["overall_hit_rate"] > current["overall_hit_rate"] + 3:
        lines.append(
            f"💡 **建议**：最优阈值 buy={best['buy_score']:.0f} hold={best['hold_score']:.0f}，"
            f"综合命中率 {best['overall_hit_rate']}% vs 当前 {current['overall_hit_rate']}%，"
            f"提升 {best['overall_hit_rate'] - current['overall_hit_rate']:.1f}%"
        )
    else:
        lines.append(
            f"💡 当前阈值 buy={current_buy:.0f} hold={current_hold:.0f} 综合命中率 {current['overall_hit_rate']}%，"
            f"与最优方案差距不大，维持现状"
        )

    lines.append("")
    lines.append(f"*基于 {results[0]['total_signals']} 条历史信号，5日后验证*")

    return "\n".join(lines)


def auto_apply_if_better(config_path: str, *, min_gap_pct: float = 5.0) -> dict | None:
    """Automatically apply optimal thresholds if they outperform current by >min_gap_pct%.
    
    Returns the applied change dict or None if no change made.
    """
    signals = load_signals()
    if len(signals) < 10:
        return None  # Not enough data
    
    from .config import load_config
    config = load_config(config_path)
    current_buy = config.monitor.decision_thresholds.buy_score
    current_hold = config.monitor.decision_thresholds.hold_score
    
    current = backtest_thresholds(signals, current_buy, current_hold)
    results = grid_search(signals)
    
    if not results:
        return None
    
    best = results[0]
    gap = best["overall_hit_rate"] - current["overall_hit_rate"]
    
    if gap <= min_gap_pct:
        return None  # Not worth changing
    
    import re
    config_path_obj = Path(config_path)
    raw = config_path_obj.read_text(encoding="utf-8")
    
    new_buy = int(best["buy_score"])
    new_hold = int(best["hold_score"])
    
    raw = re.sub(r"buy_score:\s*\d+", f"buy_score: {new_buy}", raw)
    raw = re.sub(r"hold_score:\s*\d+", f"hold_score: {new_hold}", raw)
    
    # Backup
    bak = config_path_obj.with_suffix(".yaml.bak")
    config_path_obj.rename(bak)
    config_path_obj.write_text(raw, encoding="utf-8")
    
    return {
        "applied": True,
        "old_buy": current_buy,
        "new_buy": new_buy,
        "old_hold": current_hold,
        "new_hold": new_hold,
        "gap_pct": round(gap, 1),
        "old_hit_rate": current["overall_hit_rate"],
        "new_hit_rate": best["overall_hit_rate"],
    }


def main():
    parser = argparse.ArgumentParser(description="阈值优化回测")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--report", action="store_true", help="仅输出报告")
    args = parser.parse_args()

    from stock_advisor.config import load_config
    config = load_config(args.config)
    current_buy = config.monitor.decision_thresholds.buy_score
    current_hold = config.monitor.decision_thresholds.hold_score

    signals = load_signals()
    if not signals:
        print("无历史信号数据")
        return

    results = grid_search(signals)
    report = format_report(results, current_buy, current_hold)
    print(report)


if __name__ == "__main__":
    main()
