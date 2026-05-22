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
from .feedback_loop import log_debate_result, get_weighted_confidence

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
        "focus": "短期动量信号：价格突破、成交量放大、资金净流入、板块联动、游资动向",
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
        "focus": "风险指标：最大回撤、止损距离、仓位集中度、下行风险、黑天鹅概率",
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
        "focus": "技术指标信号：MA排列方向、MACD金叉死叉、RSI超买超卖区间、布林带位置、支撑阻力位",
        "temperature": 0.5,
    },
    "宏观观察": {
        "system": """你是A股"宏观观察"——宏观策略分析师。你的信条：
- 个股离不开大盘，板块轮动决定方向
- 政策风向是第一生产力
- 流动性是市场的血液
- 关注：大盘指数趋势、板块资金轮动、货币政策信号、产业政策利好
- 你的风格是"自上而下，先看天再下地"

A股铁律：最小交易单位100股（1手），建议数量必须是100的整数倍。
输出格式（严格遵守）：动作:买/卖/持有|数量:XXX股|价格:XX-XX|信心:0.X|理由:一句话""",
        "focus": "宏观环境：大盘趋势方向、市场情绪、板块轮动、政策催化、流动性松紧",
        "temperature": 0.3,
    },
    "资金猎犬": {
        "system": """你是A股"资金猎犬"——资金流向追踪专家。你的信条：
- 资金是股价的唯一驱动力
- 主力资金的动向比任何技术指标都真实
- 北向资金、融资余额、大宗交易是明牌
- 关注：主力净流入/流出、北向资金持仓变化、龙虎榜上榜、大宗交易折溢价
- 你的风格是"跟着钱走，钱在哪我在哪"

A股铁律：最小交易单位100股（1手），建议数量必须是100的整数倍。
输出格式（严格遵守）：动作:买/卖/持有|数量:XXX股|价格:XX-XX|信心:0.X|理由:一句话""",
        "focus": "资金信号：主力净流向、成交量异动、北向资金动向、大单成交占比、龙虎榜席位",
        "temperature": 0.5,
    },
}

ARBITER_SYSTEM = """你是A股交易"仲裁官"。五位分析师（大胆猎手、铁血风控、趋势判官、宏观观察、资金猎犬）各给出了意见。
请综合多方观点，输出最终决策。

决策规则（严格按优先级）：
1. ⚠️ 铁血风控信心>0.8 → 必须采纳风控意见，无视其他方（本金安全最高）
2. 4+方一致 → 直接执行
3. 3方一致 → 执行多数意见，仓位7成
4. 2方一致 → 执行少数派意见，仓位减半
5. 各方分歧 → 持有不动（hold）
6. 数量必须是100的整数倍，0股=不动

输出格式（严格遵守，用|分隔各字段）：
动作:买/卖/持有|数量:XXX股|价格:XX.XX-XX.XX|信心:0.X|投票:猎手:买/卖/持有|风控:买/卖/持有|判官:买/卖/持有|宏观:买/卖/持有|猎犬:买/卖/持有|理由:综合判断理由（≤100字）"""


# ═══════════════════════════════════════════════════════════════
# Core logic
# ═══════════════════════════════════════════════════════════════

def _call_agent(
    role_name: str,
    role_config: dict,
    stock_context: str,
    timeout: int = 25,
    focus_hint: str = "",
) -> AgentOpinion | None:
    """Call a single agent with role-specific prompt and focus area."""
    try:
        user_msg = f"分析以下股票并给出操作建议：\n\n{stock_context}"
        if focus_hint:
            user_msg += f"\n\n【你的专业领域】请重点分析{focus_hint}"

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
                    {"role": "user", "content": user_msg},
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


def _pre_arbiter_rules(
    opinions: list[AgentOpinion],
    name: str,
    symbol: str,
    current_price: Decimal,
) -> MultiAgentDecision | None:
    """Check if we can short-circuit without calling the arbiter.

    Two rules, enforced in code (not relying on LLM to follow them):
    1. 风控 confidence > 0.8 + says sell → VETO, force sell immediately
    2. All responding agents agree → UNANIMOUS, skip arbiter
    """
    if not opinions:
        return None

    # Rule 1: 风控强制否决 (safety override — budget protection above all)
    for o in opinions:
        if o.role == "铁血风控" and o.confidence > 0.8 and o.action == "sell":
            logger.info(
                "🛡 风控否决 %s：信心%.2f，跳过仲裁强制执行卖出",
                name, o.confidence,
            )
            return MultiAgentDecision(
                action="sell",
                quantity=o.quantity,
                price_range=o.price_hint,
                confidence=o.confidence,
                vote_summary="风控否决（跳过仲裁）",
                reasoning=f"风控官强制执行卖出：{o.reasoning}",
                risk_warnings=[f"⚠️ 风控否决: {o.reasoning}"],
                agent_opinions=opinions,
            )

    # Rule 2: ≥3 agents agree → skip arbiter (majority with 5-agent panel)
    if len(opinions) >= 3:
        actions = [o.action for o in opinions]
        action_counts = {}
        for a in actions:
            action_counts[a] = action_counts.get(a, 0) + 1
        top_action = max(action_counts, key=action_counts.get)
        top_count = action_counts[top_action]
        
        if top_count >= 3:
            # 3+ agree → strong consensus
            agreeing = [o for o in opinions if o.action == top_action]
            avg_conf = sum(o.confidence for o in agreeing) / len(agreeing)
            if top_action == "buy":
                qty = max(o.quantity for o in agreeing)
            elif top_action == "sell":
                qty = max(o.quantity for o in agreeing)
            else:
                qty = 0
            logger.info(
                "⚡ 多数一致 %s → %s（%d/%d agent，跳过仲裁省一次API）",
                name, top_action, top_count, len(opinions),
            )
            vote_detail = " | ".join(f"{o.role}:{o.action}" for o in opinions)
            return MultiAgentDecision(
                action=top_action,
                quantity=qty,
                price_range=agreeing[0].price_hint,
                confidence=avg_conf,
                vote_summary=vote_detail,
                reasoning=f"{'买入' if top_action == 'buy' else '卖出' if top_action == 'sell' else '持有'}（{top_count}/{len(opinions)} agent一致，跳过仲裁）",
                risk_warnings=[],
                agent_opinions=opinions,
            )

    return None  # No strong consensus, need arbiter


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

    # Phase 1: Call all 5 agents in parallel with role-specific focus
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(
                _call_agent,
                role_name,
                role_config,
                context,
                timeout=timeout_per_agent,
                focus_hint=role_config.get("focus", ""),
            ): role_name
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
                # Apply learned agent weight to confidence
                opinion.confidence = round(get_weighted_confidence(role_name, opinion.confidence), 2)
                opinions.append(opinion)
                logger.info("Agent %s: action=%s qty=%d confidence=%.2f (weighted)", role_name, opinion.action, opinion.quantity, opinion.confidence)

    if len(opinions) < 1:
        logger.warning("Multi-agent debate: no agents responded, aborting")
        return None

    # Phase 1.5: Pre-arbiter rules — short-circuit when possible
    pre_ruling = _pre_arbiter_rules(opinions, name, symbol, current_price)
    if pre_ruling is not None:
        # Log for feedback learning
        try:
            log_debate_result(
                symbol=symbol,
                name=name,
                price=current_price,
                action=pre_ruling.action,
                confidence=float(pre_ruling.confidence),
                vote_summary=pre_ruling.vote_summary,
                reasoning=pre_ruling.reasoning,
                agent_votes={o.role: o.action for o in opinions},
            )
        except Exception:
            pass
        elapsed = time.time() - t0
        logger.info(
            "Multi-agent debate short-circuited in %.1fs: action=%s qty=%d confidence=%.2f",
            elapsed, pre_ruling.action, pre_ruling.quantity, pre_ruling.confidence,
        )
        return pre_ruling

    # Phase 2: Arbiter synthesizes (only called when there's real disagreement)
    opinion_text = "\n\n".join(
        f"【{o.role}】动作:{o.action} 数量:{o.quantity}股 信心:{o.confidence} 理由:{o.reasoning}"
        for o in opinions
    )

    arbiter_prompt = f"""以下为多位分析师对{name}({symbol})的操作建议：
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

        # Log for feedback learning
        try:
            log_debate_result(
                symbol=symbol,
                name=name,
                price=current_price,
                action=decision.action,
                confidence=float(decision.confidence),
                vote_summary=decision.vote_summary,
                reasoning=decision.reasoning,
                agent_votes={o.role: o.action for o in opinions},
            )
        except Exception:
            pass

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
