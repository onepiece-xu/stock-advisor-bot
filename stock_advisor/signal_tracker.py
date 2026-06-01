"""
信号准确率追踪 — Signal Accuracy Tracker

核心理念：
  - 评分引擎每次喊 buy/sell/hold 都是一个预测
  - 把历史预测存下来，对照实际走势验证
  - 统计命中率 → 指导参数调整方向
  - 买入命中率：喊 buy 后 N 日涨了多少
  - 卖出命中率：喊 sell 后 N 日跌了多少
  - 总体准确率：信号方向与实际方向一致的比例

存储格式：data/signals/signal_log.jsonl
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import requests

logger = logging.getLogger(__name__)

SIGNAL_DIR = Path(__file__).resolve().parent.parent / "data" / "signals"
SIGNAL_LOG = SIGNAL_DIR / "signal_log.jsonl"


@dataclass
class SignalRecord:
    """单次评分信号记录"""
    timestamp: str          # ISO 8601
    symbol: str
    name: str
    action: str             # buy/sell/hold/avoid/reduce
    score: float
    price: float
    confidence: str         # high/medium/low
    regime: str             # bull/bear/neutral
    rationale: str          # 核心理由（截断至100字）


@dataclass
class SignalFeedback:
    """信号验证结果"""
    record: SignalRecord
    days_later: int
    actual_price: float
    actual_move_pct: float
    was_correct: bool
    direction_match: str    # "同向✓" / "反向✗" / "中性-"


def ensure_dir() -> None:
    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)


def log_signal(
    symbol: str,
    name: str,
    action: str,
    score: Decimal | float,
    price: Decimal | float,
    confidence: str,
    regime: str,
    rationale: str,
) -> None:
    """Log a scoring engine signal for later verification."""
    ensure_dir()
    record = SignalRecord(
        timestamp=datetime.now().isoformat(),
        symbol=symbol,
        name=name,
        action=action,
        score=float(score),
        price=float(price),
        confidence=confidence,
        regime=regime,
        rationale=rationale[:100],
    )
    try:
        with open(SIGNAL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("Failed to log signal: %s", exc)


def load_recent_signals(days: int = 7) -> list[SignalRecord]:
    """Load signals from the last N days."""
    if not SIGNAL_LOG.exists():
        return []
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    records = []
    try:
        with open(SIGNAL_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("timestamp", "") >= cutoff:
                        records.append(SignalRecord(**data))
                except (json.JSONDecodeError, TypeError):
                    continue
    except Exception as exc:
        logger.warning("Failed to load signal log: %s", exc)
    return records


def _fetch_latest_price(symbol: str) -> Decimal | None:
    """Fetch current price for a stock symbol."""
    if symbol.startswith("sh"):
        market = "1"
    elif symbol.startswith("sz"):
        market = "0"
    else:
        return None
    code = symbol[2:]
    try:
        resp = requests.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={
                "secid": f"{market}.{code}",
                "fields": "f43,f57,f58",
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        price = data.get("f43")
        if price and price != "-":
            return Decimal(str(price))
    except Exception as exc:
        logger.warning("stock_advisor/signal_tracker.py:_fetch_latest_price failed: %s", exc)
    return None


def verify_signal(record: SignalRecord, days_later: int = 1) -> SignalFeedback | None:
    """Verify a signal against current actual price.
    
    Rules:
      - buy → correct if price went up > 0.5%
      - sell/reduce → correct if price went down > 0.5%
      - hold/avoid → correct if price moved < 2% (no significant move)
    """
    actual_price = _fetch_latest_price(record.symbol)
    if actual_price is None or record.price <= 0:
        return None
    
    move_pct = float((actual_price - Decimal(str(record.price))) / Decimal(str(record.price)) * 100)
    
    if record.action in ("buy",):
        correct = move_pct > 0.5
        direction = "同向✓" if correct else "反向✗"
    elif record.action in ("sell", "reduce"):
        correct = move_pct < -0.5
        direction = "同向✓" if correct else "反向✗"
    else:
        correct = abs(move_pct) < 2.0
        direction = "中性-" if correct else f"异动{'+' if move_pct>0 else ''}{move_pct:.1f}%"
    
    return SignalFeedback(
        record=record,
        days_later=days_later,
        actual_price=float(actual_price),
        actual_move_pct=move_pct,
        was_correct=correct,
        direction_match=direction,
    )


def evaluate_signal_accuracy(days_lookback: int = 7) -> dict:
    """Evaluate all recent signals and return accuracy stats.
    
    Returns dict with:
      - buy_hit_rate: buy信号的命中率
      - sell_hit_rate: sell信号的命中率
      - overall_accuracy: 总体准确率
      - total_signals: 验证信号总数
      - buy_count, sell_count, hold_count
      - feedbacks: list of SignalFeedback
    """
    records = load_recent_signals(days=days_lookback)
    if not records:
        return {"total_signals": 0, "message": "无历史信号"}
    
    # Deduplicate: keep only latest signal per symbol per day
    deduped = {}
    for r in records:
        key = f"{r.symbol}_{r.timestamp[:10]}"
        deduped[key] = r
    unique_records = list(deduped.values())
    
    feedbacks = []
    for r in unique_records:
        fb = verify_signal(r, days_later=1)
        if fb:
            feedbacks.append(fb)
    
    if not feedbacks:
        return {"total_signals": 0, "message": "无法获取现价验证"}
    
    buy_fbs = [f for f in feedbacks if f.record.action == "buy"]
    sell_fbs = [f for f in feedbacks if f.record.action in ("sell", "reduce")]
    
    buy_hit = sum(1 for f in buy_fbs if f.was_correct)
    sell_hit = sum(1 for f in sell_fbs if f.was_correct)
    total_correct = sum(1 for f in feedbacks if f.was_correct)
    
    return {
        "total_signals": len(feedbacks),
        "buy_count": len(buy_fbs),
        "buy_hit_rate": round(buy_hit / len(buy_fbs) * 100, 1) if buy_fbs else None,
        "sell_count": len(sell_fbs),
        "sell_hit_rate": round(sell_hit / len(sell_fbs) * 100, 1) if sell_fbs else None,
        "overall_accuracy": round(total_correct / len(feedbacks) * 100, 1),
        "feedbacks": feedbacks,
    }


def format_accuracy_report(stats: dict) -> str:
    """Format accuracy statistics as markdown."""
    if stats.get("total_signals", 0) == 0:
        return f"📊 信号准确率：{stats.get('message', '暂无数据')}"
    
    lines = ["📊 **评分引擎信号准确率**"]
    lines.append("")
    lines.append(f"| 维度 | 统计 |")
    lines.append(f"|------|------|")
    lines.append(f"| 总验证信号 | {stats['total_signals']} 条 |")
    
    if stats.get("buy_hit_rate") is not None:
        emoji = "✅" if stats["buy_hit_rate"] >= 60 else "⚠️" if stats["buy_hit_rate"] >= 40 else "❌"
        lines.append(f"| 买入命中率 | {emoji} {stats['buy_hit_rate']:.0f}%（{stats['buy_count']}条） |")
    else:
        lines.append(f"| 买入命中率 | — 无买入信号 |")
    
    if stats.get("sell_hit_rate") is not None:
        emoji = "✅" if stats["sell_hit_rate"] >= 60 else "⚠️" if stats["sell_hit_rate"] >= 40 else "❌"
        lines.append(f"| 卖出命中率 | {emoji} {stats['sell_hit_rate']:.0f}%（{stats['sell_count']}条） |")
    else:
        lines.append(f"| 卖出命中率 | — 无卖出信号 |")
    
    emoji = "✅" if stats["overall_accuracy"] >= 60 else "⚠️" if stats["overall_accuracy"] >= 40 else "❌"
    lines.append(f"| **总体准确率** | {emoji} **{stats['overall_accuracy']:.0f}%** |")
    
    return "\n".join(lines)
