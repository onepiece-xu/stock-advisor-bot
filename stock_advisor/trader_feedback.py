"""
Trader Feedback Module — 交易者行为与系统信号对齐分析

核心理念：
  把用户的实际交易（trade journal）和系统评分信号（signal log）做对比，
  统计"系统说买，用户也买" vs "系统说买，用户却卖"的对齐率。

工作流：
  1. 读取 trades.jsonl → 每条交易有 side (buy/sell), code, price, created_at
  2. 对每条交易，在 signal_log.jsonl 中找到该交易时间之前、同一标的的最新信号
  3. 判断对齐度：
     - buy 交易 + buy 信号 → aligned（强化）
     - sell 交易 + buy/hold 信号 → contradicted（系统看多但用户卖了）
     - buy 交易 + sell/reduce/avoid 信号 → contradicted（系统看空但用户买了）
     - sell 交易 + sell/reduce 信号 → aligned（一致看空）
  4. 存储到 data/feedback/trader_feedback.jsonl
  5. 生成对齐统计报告

存储格式：data/feedback/trader_feedback.jsonl
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

FEEDBACK_DIR = Path(__file__).resolve().parent.parent / "data" / "feedback"
TRADER_FEEDBACK_LOG = FEEDBACK_DIR / "trader_feedback.jsonl"
TRADE_JOURNAL_PATH = Path(__file__).resolve().parent.parent / "data" / "portfolio" / "trade_journal" / "trades.jsonl"
SIGNAL_LOG = Path(__file__).resolve().parent.parent / "data" / "signals" / "signal_log.jsonl"

AlignmentKind = Literal["aligned", "contradicted", "neutral", "no_signal"]


@dataclass
class TraderFeedbackEntry:
    """单条交易-信号对比结果"""
    trade_id: str                # trade journal entry_id
    symbol: str                  # 股票代码
    name: str                    # 股票名称
    trade_side: str              # buy / sell
    trade_price: float
    trade_quantity: int
    trade_timestamp: str         # ISO datetime of the trade
    signal_action: str | None    # 最近信号的 action，None 表示无信号
    signal_score: float | None
    signal_price: float | None
    signal_timestamp: str | None
    alignment: AlignmentKind    # aligned / contradicted / neutral / no_signal
    note: str                    # 可读说明
    evaluated_at: str            # ISO datetime of evaluation


def ensure_dir() -> None:
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)


def _load_trades(limit: int = 50) -> list[dict]:
    """Load trade journal entries, newest first, limited."""
    if not TRADE_JOURNAL_PATH.exists():
        return []
    entries = []
    try:
        with open(TRADE_JOURNAL_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        logger.warning("Failed to load trade journal: %s", exc)
        return []

    # Sort by created_at descending, take most recent
    entries.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return entries[:limit]


def _load_signals() -> list[dict]:
    """Load all signal log entries."""
    if not SIGNAL_LOG.exists():
        return []
    entries = []
    try:
        with open(SIGNAL_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        logger.warning("Failed to load signal log: %s", exc)
        return []
    # Sort chronologically for binary-search lookups
    entries.sort(key=lambda e: e.get("timestamp", ""))
    return entries


def _find_closest_signal(signals: list[dict], symbol: str, before_ts: str) -> dict | None:
    """Find the most recent signal for `symbol` with timestamp < `before_ts`.

    Signals must be sorted chronologically.
    Returns the signal dict with the largest timestamp < before_ts, or None.
    """
    best = None
    for s in signals:
        if s.get("symbol") != symbol:
            continue
        ts = s.get("timestamp", "")
        if ts >= before_ts:
            break  # Signals are sorted, no more candidates before before_ts
        best = s
    return best


def _determine_alignment(trade_side: str, signal_action: str | None) -> AlignmentKind:
    """Compare trade side with signal action to determine alignment."""
    if signal_action is None:
        return "no_signal"

    if trade_side == "buy":
        if signal_action == "buy":
            return "aligned"
        elif signal_action in ("sell", "reduce"):
            return "contradicted"
        else:  # hold, avoid
            return "neutral"
    elif trade_side == "sell":
        if signal_action in ("sell", "reduce"):
            return "aligned"
        elif signal_action == "buy":
            return "contradicted"
        else:  # hold, avoid
            return "neutral"
    return "neutral"


def _build_note(trade_side: str, signal_action: str | None, alignment: AlignmentKind) -> str:
    """Generate human-readable alignment note."""
    if alignment == "no_signal":
        return f"交易前无系统信号记录"
    if alignment == "aligned":
        return f"系统建议{signal_action}，用户{trade_side}，✓ 一致"
    if alignment == "contradicted":
        return f"系统建议{signal_action}，用户{trade_side}，✗ 矛盾"
    return f"系统建议{signal_action}，用户{trade_side}，- 中性"


def evaluate_trades() -> list[TraderFeedbackEntry]:
    """Evaluate all recent trades against system signals.

    For each trade in the journal, finds the most recent system signal
    before the trade timestamp and determines alignment.

    Returns a list of feedback entries.
    """
    trades = _load_trades(limit=50)
    if not trades:
        logger.info("No trades found in journal")
        return []

    signals = _load_signals()
    if not signals:
        logger.info("No signals found in signal log")
        # Still evaluate trades but mark as no_signal
        signals = []

    now_ts = datetime.now().isoformat()
    results = []

    for trade in trades:
        symbol = trade.get("symbol", "")
        if not symbol:
            continue

        trade_ts = trade.get("created_at", "")
        trade_side = trade.get("side", "")

        # Find closest signal before this trade
        signal = _find_closest_signal(signals, symbol, trade_ts)

        signal_action = signal.get("action") if signal else None
        signal_score = signal.get("score") if signal else None
        signal_price = signal.get("price") if signal else None
        signal_ts = signal.get("timestamp") if signal else None

        alignment = _determine_alignment(trade_side, signal_action)
        note = _build_note(trade_side, signal_action, alignment)

        entry = TraderFeedbackEntry(
            trade_id=trade.get("entry_id", ""),
            symbol=symbol,
            name=trade.get("name", ""),
            trade_side=trade_side,
            trade_price=trade.get("price", 0),
            trade_quantity=trade.get("quantity", 0),
            trade_timestamp=trade_ts,
            signal_action=signal_action,
            signal_score=signal_score,
            signal_price=signal_price,
            signal_timestamp=signal_ts,
            alignment=alignment,
            note=note,
            evaluated_at=now_ts,
        )
        results.append(entry)

    return results


def compute_alignment_stats(feedbacks: list[TraderFeedbackEntry] | None = None) -> dict:
    """Compute alignment statistics from trader feedback entries.

    Args:
        feedbacks: Pre-computed feedback entries. If None, calls evaluate_trades().

    Returns dict with:
      - total_trades: number of trades evaluated
      - aligned_count, contradicted_count, neutral_count, no_signal_count
      - alignment_rate: aligned / (aligned + contradicted) * 100
      - buy_stats: {total, aligned, contradicted, neutral, no_signal}
      - sell_stats: {total, aligned, contradicted, neutral, no_signal}
      - feedbacks: list of feedback entries
    """
    if feedbacks is None:
        feedbacks = evaluate_trades()

    total = len(feedbacks)
    aligned = sum(1 for f in feedbacks if f.alignment == "aligned")
    contradicted = sum(1 for f in feedbacks if f.alignment == "contradicted")
    neutral = sum(1 for f in feedbacks if f.alignment == "neutral")
    no_signal = sum(1 for f in feedbacks if f.alignment == "no_signal")

    # Per-side breakdown
    buy_fbs = [f for f in feedbacks if f.trade_side == "buy"]
    sell_fbs = [f for f in feedbacks if f.trade_side == "sell"]

    def _side_stats(fbs):
        return {
            "total": len(fbs),
            "aligned": sum(1 for f in fbs if f.alignment == "aligned"),
            "contradicted": sum(1 for f in fbs if f.alignment == "contradicted"),
            "neutral": sum(1 for f in fbs if f.alignment == "neutral"),
            "no_signal": sum(1 for f in fbs if f.alignment == "no_signal"),
        }

    non_trivial = aligned + contradicted
    alignment_rate = round(aligned / non_trivial * 100, 1) if non_trivial > 0 else None

    return {
        "total_trades": total,
        "aligned_count": aligned,
        "contradicted_count": contradicted,
        "neutral_count": neutral,
        "no_signal_count": no_signal,
        "alignment_rate": alignment_rate,
        "buy_stats": _side_stats(buy_fbs),
        "sell_stats": _side_stats(sell_fbs),
        "feedbacks": feedbacks,
    }


def format_alignment_report(stats: dict, max_show: int = 5) -> str:
    """Format alignment statistics as a readable markdown section.

    Args:
        stats: Output from compute_alignment_stats()
        max_show: Max number of individual feedback entries to show in detail
    """
    total = stats.get("total_trades", 0)
    if total == 0:
        return "🤝 **交易者-系统对齐**: 暂无交易记录可供分析。"

    aligned = stats.get("aligned_count", 0)
    contradicted = stats.get("contradicted_count", 0)
    neutral = stats.get("neutral_count", 0)
    no_sig = stats.get("no_signal_count", 0)
    alignment_rate = stats.get("alignment_rate")

    lines = ["🤝 **交易者 vs 系统信号对齐分析**"]
    lines.append("")
    lines.append(f"| 维度 | 统计 |")
    lines.append(f"|------|------|")
    lines.append(f"| 总交易数 | {total} 笔 |")
    lines.append(f"| 对齐（一致） | ✅ {aligned} 笔 |")
    lines.append(f"| 矛盾（相悖） | ❌ {contradicted} 笔 |")
    lines.append(f"| 中性 | — {neutral} 笔 |")
    lines.append(f"| 无信号参考 | ? {no_sig} 笔 |")

    if alignment_rate is not None:
        if alignment_rate >= 70:
            emoji = "🟢"
            comment = "系统信号与实际操作高度一致"
        elif alignment_rate >= 50:
            emoji = "🟡"
            comment = "系统信号与实际操作基本一致"
        else:
            emoji = "🔴"
            comment = "系统信号与实际操作分歧较大，需要关注"
        lines.append(f"| **对齐率** | {emoji} **{alignment_rate:.0f}%** — {comment} |")

    # Buy-side breakdown
    buy = stats.get("buy_stats", {})
    if buy.get("total", 0) > 0:
        lines.append("")
        lines.append("**买入对齐**:")
        lines.append(f"- 共 {buy['total']} 笔买入，其中 {buy['aligned']} 笔与系统 buy 信号一致，{buy['contradicted']} 笔与系统看空信号矛盾")

    sell = stats.get("sell_stats", {})
    if sell.get("total", 0) > 0:
        lines.append("")
        lines.append("**卖出对齐**:")
        lines.append(f"- 共 {sell['total']} 笔卖出，其中 {sell['aligned']} 笔与系统 sell/reduce 信号一致，{sell['contradicted']} 笔与系统 buy 信号矛盾（系统看多但用户卖出）")

    # Show individual contradictions
    contradictions = [f for f in stats.get("feedbacks", []) if f.alignment == "contradicted"]
    if contradictions:
        lines.append("")
        lines.append("**⚠️ 矛盾交易明细**:")
        for fb in contradictions[:max_show]:
            symbol_display = f"{fb.name}({fb.symbol})" if fb.name else fb.symbol
            lines.append(f"- {symbol_display}: {fb.note} | 交易价 {fb.trade_price} | 信号价 {fb.signal_price}")

    return "\n".join(lines)


def save_trader_feedback(feedbacks: list[TraderFeedbackEntry]) -> None:
    """Persist trader feedback entries to JSONL file.

    Deduplicates by trade_id — only appends new entries.
    """
    ensure_dir()

    # Load existing trade_ids to avoid duplicates
    existing_ids: set[str] = set()
    if TRADER_FEEDBACK_LOG.exists():
        try:
            with open(TRADER_FEEDBACK_LOG, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        existing_ids.add(data.get("trade_id", ""))
                    except json.JSONDecodeError:
                        continue
        except Exception as exc:
            logger.warning("stock_advisor/trader_feedback.py:save_trader_feedback failed: %s", exc)

    new_count = 0
    try:
        with open(TRADER_FEEDBACK_LOG, "a", encoding="utf-8") as f:
            for fb in feedbacks:
                if fb.trade_id in existing_ids:
                    continue
                f.write(json.dumps(asdict(fb), ensure_ascii=False) + "\n")
                new_count += 1
        if new_count > 0:
            logger.info("Saved %d new trader feedback entries", new_count)
    except Exception as exc:
        logger.warning("Failed to save trader feedback: %s", exc)


def run_trader_feedback() -> str:
    """Main entry point: evaluate trades, save feedback, return report string.

    Called from close review to append alignment section.
    """
    try:
        feedbacks = evaluate_trades()
        if feedbacks:
            save_trader_feedback(feedbacks)
        stats = compute_alignment_stats(feedbacks)
        return format_alignment_report(stats)
    except Exception as exc:
        logger.warning("Trader feedback generation failed: %s", exc)
        return ""
