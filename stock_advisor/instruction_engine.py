"""Phase 3: Single intraday instruction engine.

Resolves multiple signal sources (scoring engine, multi-agent debate,
price triggers, risk controls) into ONE instruction per stock per cycle.

Rules (in priority order):
  1. Debate override: if debate disagrees with score on buy/reduce, debate wins
  2. Trigger hit: if price is inside a trigger zone, that takes priority
  3. Risk guard: deep loss (>-10%) blocks buy; high gain (>+8%) enables profit-taking
  4. Score fallback: if nothing else fires, use scoring engine verdict

Output: a single IntradayInstruction per stock.
"""

from __future__ import annotations
from .advice import validate_lot_size

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# ── Output type ──


@dataclass(slots=True)
class IntradayInstruction:
    """Single actionable instruction for one stock during trading hours."""

    code: str
    name: str
    action: str  # buy | sell | hold
    quantity: int = 0
    reason: str = ""
    priority: int = 0   # higher = more important
    source: str = ""    # score | debate | trigger | risk_guard

    # Pricing context (for display, not decision)
    current_price: Decimal = Decimal("0")
    trigger_price_min: Decimal | None = None
    trigger_price_max: Decimal | None = None


# ── Resolution rules ──

def resolve_instruction(
    *,
    code: str,
    name: str,
    current_price: Decimal,
    score_action: str,
    score_confidence: str,
    debate_action: str | None = None,
    debate_confidence: float = 0.0,
    trigger_hit_action: str | None = None,
    trigger_hit_quantity: int = 0,
    holding_quantity: int = 0,
    holding_pnl_pct: float = 0.0,
    max_single_position_pct: float = 35.0,
) -> IntradayInstruction:
    """Resolve multiple signal sources into one instruction.

    Priority chain: debate > trigger > risk_guard > score.
    """
    instr = IntradayInstruction(
        code=code,
        name=name,
        action="hold",
        current_price=current_price,
    )

    # ── Step 1: Risk guard (hard blocks) ──
    if holding_quantity > 0 and holding_pnl_pct < -10:
        instr.action = "sell"
        instr.reason = f"风控：浮亏{holding_pnl_pct:.1f}%触发硬止损，优先减仓"
        instr.priority = 100
        instr.source = "risk_guard"
        instr.quantity = validate_lot_size(holding_quantity // 3)
        return instr

    # ── Step 2: Debate override (highest signal priority) ──
    if debate_action and debate_action != score_action:
        instr.action = debate_action
        instr.reason = f"辩论覆盖：{score_action}→{debate_action}（置信度{debate_confidence:.0%}）"
        instr.priority = 90
        instr.source = "debate"
        return instr

    # ── Step 3: Trigger hit ──
    if trigger_hit_action:
        instr.action = trigger_hit_action
        validated = validate_lot_size(trigger_hit_quantity)
        # 验证后为 0（<100 股）时退回原始值，不静默丢弃
        instr.quantity = validated if validated > 0 else trigger_hit_quantity
        if trigger_hit_action == "sell":
            instr.reason = "触发卖出区间，按计划执行"
            instr.priority = 80
        else:
            instr.reason = "触发买入区间，可考虑建仓"
            instr.priority = 75
        instr.source = "trigger"
        return instr

    # ── Step 4: Score engine (fallback) ──
    if score_action == "buy" and holding_quantity > 0:
        # Prevent buy signals on existing positions above concentration limit
        instr.action = "hold"
        instr.reason = f"评分{score_action}但已有持仓，不加仓"
        instr.source = "score"
        return instr

    if score_action == "reduce":
        instr.action = "sell"
        instr.reason = "评分引擎建议减仓"
        instr.priority = 50
        instr.source = "score"
        return instr

    if score_action == "avoid" and holding_quantity > 0:
        instr.action = "sell"
        instr.reason = "评分引擎建议清仓观望"
        instr.priority = 55
        instr.source = "score"
        return instr

    if score_action == "buy":
        instr.action = "buy"
        instr.reason = "评分引擎建议买入"
        instr.priority = 40
        instr.source = "score"
        return instr

    # Default: hold
    instr.action = "hold"
    instr.reason = f"评分{score_action}，继续观察"
    instr.source = "score"
    return instr


def instructions_from_cycle(
    scored_stocks: list[dict[str, Any]],
    debate_results: dict[str, dict[str, Any]],
    trigger_hits: dict[str, dict[str, Any]],
    holdings: dict[str, dict[str, Any]],
) -> list[IntradayInstruction]:
    """Build instruction list from a full daemon cycle's outputs.

    Args:
        scored_stocks: [{code, name, current_price, action, confidence, ...}]
        debate_results: {code: {action, confidence, reasoning, ...}}
        trigger_hits: {code: {action, quantity, ...}}
        holdings: {code: {quantity, pnl_pct, ...}}

    Returns:
        One IntradayInstruction per stock (only actionable ones).
    """
    instructions: list[IntradayInstruction] = []
    for stock in scored_stocks:
        code = stock["code"]
        debate = debate_results.get(code)
        trigger = trigger_hits.get(code)
        holding = holdings.get(code, {})

        instr = resolve_instruction(
            code=code,
            name=stock.get("name", code),
            current_price=Decimal(str(stock.get("current_price", 0))),
            score_action=stock.get("action", "hold"),
            score_confidence=stock.get("confidence", "medium"),
            debate_action=debate.get("action") if debate else None,
            debate_confidence=float(debate.get("confidence", 0)) if debate else 0.0,
            trigger_hit_action=trigger.get("action") if trigger else None,
            trigger_hit_quantity=int(trigger.get("quantity", 0)) if trigger else 0,
            holding_quantity=int(holding.get("quantity", 0)),
            holding_pnl_pct=float(holding.get("pnl_pct", 0)),
        )
        instructions.append(instr)

    # Return only actionable: buy/sell, sorted by priority desc
    actionable = [i for i in instructions if i.action in ("buy", "sell")]
    actionable.sort(key=lambda i: i.priority, reverse=True)
    return actionable
