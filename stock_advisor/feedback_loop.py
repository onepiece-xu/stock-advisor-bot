"""
每日自反馈回路 — Daily Self-Feedback Loop

灵感来源：llm-agent-trader (⭐367) 的 daily feedback 机制

工作流：
  1. 每次辩论后自动存储结果 → data/feedback/debate_log.jsonl
  2. 收盘后加载今日辩论，对照实际走势验证
  3. 计算各Agent命中率，更新权重
  4. 下次辩论时，准确率高的Agent意见自动加权

Agent权重存储在 data/feedback/agent_weights.json
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

# Default agent weights — start equal, learn over time
DEFAULT_WEIGHTS = {
    "大胆猎手": 1.0,
    "铁血风控": 1.0,
    "趋势判官": 1.0,
    "宏观观察": 0.8,   # New agents start with lower weight
    "资金猎犬": 0.8,
}

FEEDBACK_DIR = Path(__file__).resolve().parent.parent / "data" / "feedback"
DEBATE_LOG = FEEDBACK_DIR / "debate_log.jsonl"
WEIGHTS_FILE = FEEDBACK_DIR / "agent_weights.json"


@dataclass
class DebateRecord:
    """单次辩论记录"""
    timestamp: str          # ISO 8601
    symbol: str
    name: str
    price: float
    action: str             # buy/sell/hold
    confidence: float
    vote_summary: str
    reasoning: str
    agent_votes: dict[str, str]  # {agent_name: action}


@dataclass
class FeedbackResult:
    """单条反馈验证结果"""
    record: DebateRecord
    actual_move_pct: float      # 实际涨跌幅%
    was_correct: bool           # 预测是否正确
    detail: str                 # 可读说明


def ensure_dir() -> None:
    """Ensure feedback directory exists."""
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)


def log_debate_result(
    symbol: str,
    name: str,
    price: Decimal,
    action: str,
    confidence: float,
    vote_summary: str,
    reasoning: str,
    agent_votes: dict[str, str],
) -> None:
    """Log a debate result to the JSONL file."""
    ensure_dir()
    record = DebateRecord(
        timestamp=datetime.now().isoformat(),
        symbol=symbol,
        name=name,
        price=float(price),
        action=action,
        confidence=confidence,
        vote_summary=vote_summary,
        reasoning=reasoning,
        agent_votes=agent_votes,
    )
    try:
        with open(DEBATE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("Failed to log debate result: %s", exc)


def load_today_debates(today: date | None = None) -> list[DebateRecord]:
    """Load today's debate records from the log."""
    if today is None:
        today = date.today()
    today_str = today.isoformat()
    records = []
    if not DEBATE_LOG.exists():
        return records
    try:
        with open(DEBATE_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("timestamp", "").startswith(today_str):
                        records.append(DebateRecord(**data))
                except (json.JSONDecodeError, TypeError):
                    continue
    except Exception as exc:
        logger.warning("Failed to load debate log: %s", exc)
    return records


def load_agent_weights() -> dict[str, float]:
    """Load current agent weights, falling back to defaults."""
    if not WEIGHTS_FILE.exists():
        return dict(DEFAULT_WEIGHTS)
    try:
        with open(WEIGHTS_FILE, "r", encoding="utf-8") as f:
            weights = json.load(f)
        # Merge with defaults (new agents won't be in saved file)
        merged = dict(DEFAULT_WEIGHTS)
        merged.update(weights)
        return merged
    except Exception:
        return dict(DEFAULT_WEIGHTS)


def save_agent_weights(weights: dict[str, float]) -> None:
    """Save agent weights to disk."""
    ensure_dir()
    try:
        with open(WEIGHTS_FILE, "w", encoding="utf-8") as f:
            json.dump(weights, f, ensure_ascii=False, indent=2)
        logger.info("Agent weights updated: %s", weights)
    except Exception as exc:
        logger.warning("Failed to save agent weights: %s", exc)


def evaluate_debate(
    record: DebateRecord,
    actual_price: Decimal,
) -> FeedbackResult:
    """Evaluate a single debate record against actual outcome.

    Rules:
      - "buy" prediction → correct if price went up
      - "sell" prediction → correct if price went down
      - "hold" prediction → correct if price moved < 1% (either direction)
    """
    if record.price <= 0:
        return FeedbackResult(record=record, actual_move_pct=0.0, was_correct=False, detail="无效价格")

    move_pct = float((actual_price - Decimal(str(record.price))) / Decimal(str(record.price)) * 100)

    if record.action == "buy":
        correct = move_pct > 0.5  # Need >0.5% up to count as correct
        detail = f"预测买入，实际{'涨' if move_pct > 0 else '跌'}{abs(move_pct):.1f}% — {'✓正确' if correct else '✗错误'}"
    elif record.action == "sell":
        correct = move_pct < -0.5  # Need >0.5% down to count as correct
        detail = f"预测卖出，实际{'跌' if move_pct < 0 else '涨'}{abs(move_pct):.1f}% — {'✓正确' if correct else '✗错误'}"
    else:  # hold
        correct = abs(move_pct) < 1.0  # Within ±1% = correct hold
        detail = f"预测持有，实际波动{abs(move_pct):.1f}% — {'✓正确（窄幅震荡）' if correct else '✗错误（方向明确）'}"

    return FeedbackResult(
        record=record,
        actual_move_pct=move_pct,
        was_correct=correct,
        detail=detail,
    )


def update_weights_from_feedback(
    feedbacks: list[FeedbackResult],
    learning_rate: float = 0.05,
) -> dict[str, float]:
    """Update agent weights based on feedback results.

    For each debate, check which agents voted correctly:
      - If agent voted "buy" and price went up → +reward
      - If agent voted "sell" and price went down → +reward
      - If agent voted "hold" and price barely moved → +reward
      - Otherwise → -penalty

    Weight adjustments are clipped to [0.3, 2.0].
    """
    weights = load_agent_weights()
    if not feedbacks:
        return weights

    for fb in feedbacks:
        actual_up = fb.actual_move_pct > 0.5
        actual_down = fb.actual_move_pct < -0.5
        actual_flat = abs(fb.actual_move_pct) < 1.0

        for agent_name, vote in fb.record.agent_votes.items():
            if agent_name not in weights:
                weights[agent_name] = DEFAULT_WEIGHTS.get(agent_name, 1.0)

            # Determine if this agent was correct
            agent_correct = False
            if vote == "buy" and actual_up:
                agent_correct = True
            elif vote == "sell" and actual_down:
                agent_correct = True
            elif vote == "hold" and actual_flat:
                agent_correct = True

            # Adjust weight
            if agent_correct:
                weights[agent_name] = min(2.0, weights[agent_name] + learning_rate)
            else:
                weights[agent_name] = max(0.3, weights[agent_name] - learning_rate * 0.5)

    # Round to 3 decimal places
    weights = {k: round(v, 3) for k, v in weights.items()}
    save_agent_weights(weights)
    return weights


def run_daily_feedback(
    actual_prices: dict[str, Decimal],  # {symbol: latest_price}
    today: date | None = None,
) -> list[FeedbackResult]:
    """Run end-of-day feedback: evaluate today's debates against actual prices.

    Args:
        actual_prices: {symbol: closing_price} for all debated stocks
        today: date to evaluate (default: today)

    Returns:
        List of feedback results for logging/reporting
    """
    records = load_today_debates(today)
    if not records:
        logger.info("No debate records to evaluate today")
        return []

    feedbacks = []
    for record in records:
        actual = actual_prices.get(record.symbol)
        if actual is None:
            continue
        fb = evaluate_debate(record, actual)
        feedbacks.append(fb)
        logger.info("Feedback %s: %s", record.name, fb.detail)

    # Update weights
    if feedbacks:
        update_weights_from_feedback(feedbacks)

    return feedbacks


def get_weighted_confidence(agent_name: str, raw_confidence: float) -> float:
    """Apply learned weight to an agent's confidence score.

    Returns adjusted confidence in [0, 1] range.
    """
    weights = load_agent_weights()
    weight = weights.get(agent_name, 1.0)
    return min(1.0, max(0.0, raw_confidence * weight))
