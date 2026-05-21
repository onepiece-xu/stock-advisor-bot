"""
多Agent辩论系统 —— 模拟分析师团队协作决策

灵感来源：aiagents-stock (⭐⭐1379) + UZI-Skill (⭐⭐1612)

架构：
  大胆猎手(Aggressive) ─┐
  铁血风控(Conservative) ├─→ 仲裁(Arbiter) ─→ 最终信号
  趋势判官(Technical)   ─┘

每个Agent收到相同数据，但系统提示词赋予不同视角。
仲裁综合三方意见，投票出最终动作（买/卖/不动）+ 执行数量。
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import requests

from .llm_analyst import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════════

@dataclass
class AgentOpinion:
    role: str                    # 角色名
    action: Literal["buy", "sell", "hold"]  # 动作
    quantity: int                # 建议数量（100的整数倍）
    price_hint: str              # 价格区间建议
    confidence: float            # 0.0-1.0
    reasoning: str               # 50字以内核心逻辑
    risk_flags: list[str]        # 风险提示

@dataclass  
class MultiAgentDecision:
    """多Agent辩论的最终决策"""
    action: Literal["buy", "sell", "hold"]
    quantity: int                # 100的整数倍
    price_range: str
    confidence: float
    vote_summary: str            # 投票分布
    reasoning: str               # 综合理由
    risk_warnings: list[str]
    agent_opinions: list[AgentOpinion]  # 原始意见用于追溯


# ═══════════════════════════════════════════════════════════════
# Agent roles
# ═══════════════════════════════════════════════════════════════

AGENT_ROLES = {
    "大胆猎手": {
        "system": """你是A股"大胆猎手"——激进派交易员。你的信条：
- 宁可追高买入也不放过突破信号
- 量价齐升就是最好的入场时机
- 浮亏<15%就继续持有等待反转
- 关注：突破新高、放量上攻、板块联动、游资接力
- 你的风格是"宁可做错也不错过"

A股铁律：最小交易单位100股（1手），建议数量必须是100的整数倍。
输出格式（严格遵守）：动作:买/卖/持有|数量:XXX股|价格:XX-XX|信心:0.X|理由:一句话""",
        "temperature": 0.7,
    },
    "铁血风控": {
        "system": """你是A股"铁血风控"——保守派风控官。你的信条：
- 本金安全第一，宁可错过也不做错
- 任何持仓浮亏>7%必须考虑止损
- 不在下跌趋势中加仓（不接飞刀）
- 关注：止损线是否触发、仓位是否过重、大盘是否破位
- 你的风格是"活着才能继续玩"

A股铁律：最小交易单位100股（1手），建议数量必须是100的整数倍。
输出格式（严格遵守）：动作:买/卖/持有|数量:XXX股|价格:XX-XX|信心:0.X|理由:一句话""",
        "temperature": 0.2,
    },
    "趋势判官": {
        "system": """你是A股"趋势判官"——技术分析专家。你的信条：
- 趋势是你的朋友，不逆势交易
- 日线MA排列决定大方向，分钟线找买卖点
- 量价背离是最强的反转信号
- 关注：MA排列、MACD金叉死叉、RSI超买超卖、支撑阻力位
- 你的风格是"顺势而为，不见信号不出手"

A股铁律：最小交易单位100股（1手），建议数量必须是100的整数倍。
输出格式（严格遵守）：动作:买/卖/持有|数量:XXX股|价格:XX-XX|信心:0.X|理由:一句话""",
        "temperature": 0.5,
    },
}

ARBITER_SYSTEM = """你是A股交易"仲裁官"。三位分析师（大胆猎手、铁血风控、趋势判官）各给出了意见。
请综合三方观点，输出最终决策。

决策规则（严格按优先级）：
1. ⚠️ 铁血风控信心>0.8 → 必须采纳风控意见，无视其他两方（本金安全最高）
2. 三方一致 → 直接执行
3. 两方一致 → 执行多数意见，但仓位减半
4. 三方分歧 → 持有不动（hold）
5. 数量必须是100的整数倍，0股=不动

输出格式（严格遵守，用|分隔各字段）：
动作:买/卖/持有|数量:XXX股|价格:XX.XX-XX.XX|信心:0.X|投票:猎手:买/卖/持有|风控:买/卖/持有|判官:买/卖/持有|理由:综合判断理由（≤100字）"""


# ═══════════════════════════════════════════════════════════════
# Core logic
# ═══════════════════════════════════════════════════════════════

def _call_agent(
    role_name: str,
    role_config: dict,
    stock_context: str,
    timeout: int = 25,
) -> AgentOpinion | None:
    """Call a single agent with role-specific prompt."""
    try:
        resp = requests.post(
            f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-v4-pro",
                "messages": [
                    {"role": "system", "content": role_config["system"]},
                    {"role": "user", "content": f"分析以下股票并给出操作建议：\n\n{stock_context}"},
                ],
                "max_tokens": 1000,
                "temperature": role_config.get("temperature", 0.5),
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning("Agent %s API error: %s", role_name, resp.status_code)
            return None

        content = resp.json()["choices"][0]["message"]["content"].strip()
        return _parse_agent_response(role_name, content)

    except Exception as exc:
        logger.warning("Agent %s failed: %s", role_name, exc)
        return None


def _parse_agent_response(role: str, text: str) -> AgentOpinion | None:
    """Parse agent output: 动作:买|数量:100股|价格:33-34|信心:0.8|理由:xxx"""
    try:
        parts = {}
        # Handle both \n-separated and |-separated formats
        segments = []
        for line in text.split("\n"):
            segments.extend(line.split("|"))
        for segment in segments:
            segment = segment.strip()
            if not segment:
                continue
            if ":" in segment or "：" in segment:
                sep = ":" if ":" in segment else "："
                key, val = segment.split(sep, 1)
                parts[key.strip()] = val.strip()

        action_map = {"买": "buy", "买入": "buy", "卖": "sell", "卖出": "sell", "持有": "hold", "不动": "hold"}
        action = action_map.get(parts.get("动作", ""), "hold")
        if action == "hold" and any(a in text for a in ["买入", "买入信号", "建议买入"]):
            action = "buy"
        if action == "hold" and any(s in text for s in ["卖出", "卖出信号", "建议卖出", "减仓"]):
            action = "sell"

        qty_str = parts.get("数量", "0")
        quantity = int("".join(c for c in qty_str if c.isdigit()) or "0")

        confidence = float(parts.get("信心", "0.5"))
        price_hint = parts.get("价格", "")
        reasoning = parts.get("理由", text[:60])

        return AgentOpinion(
            role=role,
            action=action,
            quantity=quantity,
            price_hint=price_hint,
            confidence=confidence,
            reasoning=reasoning[:50],
            risk_flags=[],
        )
    except Exception as exc:
        logger.warning("Failed to parse %s response: %s — %s", role, exc, text[:100])
        return None


def debate(
    symbol: str,
    name: str,
    current_price: Decimal,
    holding_info: str = "",
    market_context: str = "",
    technical_data: str = "",
    *,
    timeout_per_agent: int = 20,
) -> MultiAgentDecision | None:
    """Run multi-agent debate and return final decision.

    Args:
        symbol: Stock code (e.g. sh601698)
        name: Stock name
        current_price: Current price
        holding_info: Position info text (e.g. "持仓200股，成本34.50，浮亏-3.2%")
        market_context: Market overview
        technical_data: Technical indicators summary
    """
    context = f"""股票：{name}({symbol})
现价：{current_price}
{f'持仓：{holding_info}' if holding_info else '未持仓'}
{f'大盘：{market_context}' if market_context else ''}
{f'技术面：{technical_data}' if technical_data else ''}

请给出你的操作建议。"""

    t0 = time.time()
    opinions: list[AgentOpinion] = []

    # Phase 1: Call all 3 agents in parallel
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(_call_agent, role_name, role_config, context, timeout=timeout_per_agent): role_name
            for role_name, role_config in AGENT_ROLES.items()
        }
        for future in as_completed(futures):
            role_name = futures[future]
            try:
                opinion = future.result()
            except Exception as exc:
                logger.warning("Agent %s thread failed: %s", role_name, exc)
                opinion = None
            if opinion:
                opinions.append(opinion)
                logger.info("Agent %s: action=%s qty=%d confidence=%.2f", role_name, opinion.action, opinion.quantity, opinion.confidence)

    if len(opinions) < 1:
        logger.warning("Multi-agent debate: no agents responded, aborting")
        return None

    # Phase 2: Arbiter synthesizes
    opinion_text = "\n\n".join(
        f"【{o.role}】动作:{o.action} 数量:{o.quantity}股 信心:{o.confidence} 理由:{o.reasoning}"
        for o in opinions
    )

    arbiter_prompt = f"""以下是三位分析师对{name}({symbol})的操作建议：
现价：{current_price}

{opinion_text}

请综合判断，输出最终决策。"""

    try:
        resp = requests.post(
            f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-v4-pro",
                "messages": [
                    {"role": "system", "content": ARBITER_SYSTEM},
                    {"role": "user", "content": arbiter_prompt},
                ],
                "max_tokens": 2000,
                "temperature": 0.3,
            },
            timeout=25,
        )
        if resp.status_code != 200:
            logger.warning("Arbiter API error: %s", resp.status_code)
            # Fallback: majority vote
            return _majority_vote(opinions, name, symbol)

        content = resp.json()["choices"][0]["message"]["content"].strip()
        decision = _parse_arbiter_response(content, opinions, name, symbol)
        decision.agent_opinions = opinions

        elapsed = time.time() - t0
        logger.info("Multi-agent debate complete in %.1fs: action=%s qty=%d confidence=%.2f", elapsed, decision.action, decision.quantity, decision.confidence)
        return decision

    except Exception as exc:
        logger.warning("Arbiter failed: %s, falling back to majority vote", exc)
        return _majority_vote(opinions, name, symbol)


def _parse_arbiter_response(text: str, opinions: list[AgentOpinion], name: str, symbol: str) -> MultiAgentDecision:
    """Parse arbiter output."""
    parts = {}
    # Handle both \n-separated and |-separated formats
    segments = []
    for line in text.split("\n"):
        segments.extend(line.split("|"))
    for line in segments:
        line = line.strip()
        if not line:
            continue
        if ":" in line or "：" in line:
            sep = ":" if ":" in line else "："
            key, val = line.split(sep, 1)
            parts[key.strip()] = val.strip()

    action_map = {"买": "buy", "买入": "buy", "卖": "sell", "卖出": "sell", "持有": "hold", "不动": "hold"}
    action = action_map.get(parts.get("动作", ""), "hold")

    qty_str = parts.get("数量", "0")
    quantity = int("".join(c for c in qty_str if c.isdigit()) or "0")

    # Enforce 100-share multiple
    if quantity > 0 and quantity % 100 != 0:
        quantity = ((quantity // 100) + 1) * 100  # Round up

    confidence = float(parts.get("信心", "0.5"))
    vote_summary = parts.get("投票", "未知")
    reasoning = parts.get("理由", text[:100])

    risk_warnings = []
    for o in opinions:
        if o.confidence > 0.7 and o.action == "sell":
            risk_warnings.append(f"{o.role}强烈建议卖出: {o.reasoning}")

    return MultiAgentDecision(
        action=action,
        quantity=quantity,
        price_range=parts.get("价格", ""),
        confidence=confidence,
        vote_summary=vote_summary,
        reasoning=reasoning,
        risk_warnings=risk_warnings,
        agent_opinions=opinions,
    )


def _majority_vote(opinions: list[AgentOpinion], name: str, symbol: str) -> MultiAgentDecision:
    """Fallback: simple majority vote when arbiter fails."""
    buy_votes = sum(1 for o in opinions if o.action == "buy")
    sell_votes = sum(1 for o in opinions if o.action == "sell")
    hold_votes = sum(1 for o in opinions if o.action == "hold")

    if hold_votes >= 2 or (buy_votes == 1 and sell_votes == 1):
        action, reasoning = "hold", "三方分歧，持有观望"
    elif buy_votes >= 2:
        action, reasoning = "buy", "多数看多，量力而行"
    elif sell_votes >= 2:
        action, reasoning = "sell", "多数看空，控制风险"
    else:
        action, reasoning = "hold", "意见不明，保持不动"

    quantities = [o.quantity for o in opinions if o.quantity > 0]
    qty = min(quantities) if quantities else 0  # Use conservative quantity

    return MultiAgentDecision(
        action=action,
        quantity=qty,
        price_range="",
        confidence=0.5,
        vote_summary=f"猎手:{buy_votes}|风控:{hold_votes}|判官:{sell_votes}",
        reasoning=f"[多数决]{reasoning}",
        risk_warnings=[],
        agent_opinions=opinions,
    )
