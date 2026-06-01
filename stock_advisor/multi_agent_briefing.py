"""
每日多Agent辩论简报 — 盘前/收盘各一次

灵感：aiagents-stock (⭐⭐1379) + UZI-Skill

用法：
  python3 -m stock_advisor.cli multi-agent-debate --config config.yaml --period morning
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .market_hours import MARKET_TZ
from .multi_agent import MultiAgentDecision, debate
from .outbox import queue_notification

logger = logging.getLogger(__name__)


def run_multi_agent_briefing(
    config,
    period: str = "morning",
) -> str:
    """Run multi-agent debate on all holdings, return formatted report.

    Args:
        config: AppConfig instance
        period: "morning" or "close"
    """
    today = datetime.now(MARKET_TZ).strftime("%m/%d")
    
    # Load holdings
    holdings = _load_current_holdings(config)
    if not holdings:
        return ""

    lines = [f"## 🧠 多Agent辩论 · {today} {'盘前' if period == 'morning' else '收盘'}"]
    lines.append("_三位分析师（猎手/风控/判官）独立判断后仲裁投票_\n")

    for h in holdings:
        decision = debate(
            symbol=h["symbol"],
            name=h["name"],
            current_price=Decimal(str(h["current_price"])),
            holding_info=f"持仓{h['quantity']}股，成本{h['cost_price']}，盈亏{h.get('pnl_pct', 0):+.1f}%",
            market_context=h.get("market_wind", ""),
            technical_data=h.get("technical", ""),
            timeout_per_agent=25,
        )
        if not decision:
            lines.append(f"### {h['name']}({h['code']})")
            lines.append("多Agent辩论失败，改用单模型判断\n")
            continue

        lines.append(format_debate_result(h, decision))

    report = "\n".join(lines)
    
    # Queue for Feishu delivery
    if config.monitor.notification.feishu.enabled:
        queue_notification(
            f"多Agent辩论 · {today} {'盘前' if period == 'morning' else '收盘'}",
            report,
        )

    return report


def format_debate_result(holding: dict, decision: MultiAgentDecision) -> str:
    """Format one debate result as markdown."""
    name = holding.get("name", "")
    code = holding.get("code", "")

    action_emoji = {"buy": "🟢买入", "sell": "🔴卖出", "hold": "🟡持有"}
    action_str = action_emoji.get(decision.action, decision.action)

    lines = [f"### {name}({code}) → {action_str}"]

    if decision.quantity > 0:
        lines.append(f"- 数量：{decision.quantity}股")
    if decision.price_range:
        lines.append(f"- 价格：{decision.price_range}")
    lines.append(f"- 信心：{decision.confidence:.0%}")
    lines.append(f"- 投票：{decision.vote_summary}")
    lines.append(f"- 理由：{decision.reasoning}")

    if decision.risk_warnings:
        lines.append("- ⚠️ 风险：")
        for w in decision.risk_warnings:
            lines.append(f"  - {w}")

    # Show individual agent opinions
    lines.append("\n**各方观点：**")
    for op in decision.agent_opinions:
        emoji = {"buy": "🟢", "sell": "🔴", "hold": "🟡"}.get(op.action, "⚪")
        lines.append(f"- {emoji} **{op.role}**：{op.reasoning} (信心{op.confidence:.0%})")

    lines.append("")
    return "\n".join(lines)


def _load_current_holdings(config) -> list[dict]:
    """Load current holdings with latest prices for debate."""
    holdings = []
    try:
        raw = _load_snapshot_raw(config)
        if not raw:
            return holdings

        from .providers import TencentQuoteProvider
        tencent = TencentQuoteProvider(config.monitor)

        # Support both formats: "holdings" (array) and legacy "positions" (dict)
        holding_list = raw.get("holdings", [])
        if not holding_list:
            # Legacy format: positions dict
            positions = raw.get("positions", {})
            name_to_code = _build_name_code_map(config.monitor.stocks)
            for pos_name, pos in positions.items():
                code = name_to_code.get(pos_name, "")
                if code:
                    holding_list.append({
                        "name": pos_name,
                        "code": code,
                        "quantity": int(pos.get("shares", 0)),
                        "costPrice": float(pos.get("avg_cost", 0)),
                    })

        for h in holding_list:
            name = h.get("name", "")
            code = h.get("code", "")
            if not code:
                code = {"中国卫通": "601698", "中兴通讯": "000063", "启明星辰": "002439"}.get(name, "")
            if not code:
                logger.warning("No code for holding: %s", name)
                continue

            stock_ref = next(
                (s for s in config.monitor.stocks if s.code == code), None
            )
            if not stock_ref:
                continue

            try:
                quote = tencent.fetch_quote(stock_ref)
            except Exception as exc:
                logger.warning("Quote fetch failed for %s: %s", code, exc)
                continue

            shares = int(h.get("quantity", 0))
            if shares <= 0:
                continue

            avg_cost = float(h.get("costPrice", 0))
            if avg_cost <= 0:
                avg_cost = float(quote.current_price)

            pnl = (float(quote.current_price) - avg_cost) / avg_cost * 100 if avg_cost > 0 else 0
            holdings.append({
                "symbol": stock_ref.symbol,
                "code": code,
                "name": name,
                "quantity": shares,
                "cost_price": f"{avg_cost:.2f}",
                "current_price": f"{quote.current_price:.2f}",
                "pnl_pct": round(pnl, 1),
            })

    except Exception as exc:
        logger.warning("Multi-agent briefing failed to load holdings: %s", exc)

    return holdings


def _build_name_code_map(stocks: list) -> dict[str, str]:
    """Build name->code mapping from known holdings."""
    return {
        "中国卫通": "601698",
        "中兴通讯": "000063",
        "启明星辰": "002439",
    }


def _load_snapshot_raw(config) -> dict | None:
    """Load snapshot JSON as raw dict (handles updated/tradeDate format mismatch)."""
    import json
    path = config.snapshot_path
    if not path.exists():
        logger.warning("Snapshot not found: %s", path)
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _load_snapshot(config):
    """Load latest portfolio snapshot."""
    from .portfolio import load_snapshot
    return load_snapshot(config.snapshot_path)
