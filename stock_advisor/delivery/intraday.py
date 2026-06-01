"""Phase 4: Intraday instruction renderer — pure formatting.

Takes pre-resolved instructions and returns formatted markdown.
Does NOT fetch data, run debates, or make decisions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..instruction_engine import IntradayInstruction


def render_intraday_instruction(
    instruction: IntradayInstruction,
    *,
    holdings_line: str = "",
    timestamp: datetime | None = None,
) -> str:
    """Render a single intraday instruction as a formatted message.

    Args:
        instruction: The resolved instruction from instruction_engine
        holdings_line: Optional holdings summary line (e.g. "📊 持仓：中国卫通 1100股 成本33.94 现价31.73")
        timestamp: Optional timestamp for the instruction

    Returns:
        Formatted markdown string ready for delivery.
    """
    ts = timestamp.strftime("%m-%d %H:%M:%S") if timestamp else ""
    direction = "买入" if instruction.action == "buy" else "卖出"
    action_emoji = "🟢" if instruction.action == "buy" else "🔴"

    parts = [
        f"{action_emoji} **{instruction.name}({instruction.code})**",
        f"动作：{direction}",
        f"来源：{instruction.source}",
    ]

    if instruction.quantity > 0:
        parts.append(f"数量：{instruction.quantity}股")
    if instruction.current_price > 0:
        parts.append(f"现价：{instruction.current_price}")
    if instruction.trigger_price_min and instruction.trigger_price_max:
        parts.append(f"触发区间：{instruction.trigger_price_min}-{instruction.trigger_price_max}")

    parts.append(f"理由：{instruction.reason}")

    if holdings_line:
        parts.append(holdings_line)

    if ts:
        parts.append(f"`{ts}`")

    return "\n".join(parts)


def render_intraday_action_card(
    instructions: list[dict[str, Any]],
    *,
    timestamp: datetime | None = None,
) -> str:
    """Render a batch of intraday instructions as an action card.

    Args:
        instructions: List of {symbol, name, action, action_text, size_text, reasons}
        timestamp: Optional timestamp

    Returns:
        Formatted action card markdown.
    """
    now = timestamp or datetime.now()
    lines = [f"【盘中动作卡】{now:%H:%M}"]

    for item in instructions:
        lines.append("")
        lines.append(item.get("name", item.get("symbol", "?")))
        lines.append(f"- 类型：{item.get('card_label', '观察')}")
        if item.get("action_text"):
            lines.append(f"- 动作：{item['action_text']}")
        if item.get("size_text"):
            lines.append(f"- 执行：{item['size_text']}")
        if item.get("reasons"):
            lines.append(f"- 原因：{item['reasons']}")

    return "\n".join(lines)
