"""
威科夫量价结构分析 — Wyckoff Volume-Price Structure

灵感来源：WyckoffTradingAgent (⭐421)
核心理论：量价关系揭示主力意图
  - 吸筹区 (Accumulation): 价稳量缩 → 主力暗中吸筹
  - 派发区 (Distribution):  价滞量增 → 主力高位出货
  - 弹簧 (Spring): 假跌破支撑迅速收回 → 极强买入信号
  - 上冲 (Upthrust): 假突破阻力迅速回落 → 极强卖出信号
  - 买卖高潮 (Climax): 极端放量 + 大K线 → 趋势终结信号

所有检测基于规则引擎，零LLM延迟。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


@dataclass
class WyckoffSignal:
    """威科夫信号"""
    dimension: str
    direction: Literal["accumulation", "distribution", "neutral"]
    strength: int            # -15 to +15
    description: str
    phase: str               # 威科夫相位


# ═══════════════════════════════════════════════════════════════
# Phase Detection
# ═══════════════════════════════════════════════════════════════

def _detect_climax(
    change_pct: Decimal,
    vol_ratio: Decimal,
    amplitude_pct: Decimal,
) -> list[WyckoffSignal]:
    """检测买卖高潮 — 极端成交量+大幅波动 = 趋势衰竭信号

    Buying Climax（买盘高潮）:
      - 放量2.5x+ + 涨幅3%+ → 可能是最后的追涨盘，顶部信号
    Selling Climax（卖盘高潮）:
      - 放量2.5x+ + 跌幅3%+ → 恐慌性抛售耗尽，底部信号
    """
    signals = []
    if vol_ratio <= 0:
        return signals

    # Buying Climax: huge volume + big up move = potential exhaustion
    if vol_ratio >= Decimal("2.5") and change_pct >= Decimal("3.0"):
        signals.append(WyckoffSignal(
            "买盘高潮", "distribution", -12,
            f"放量{vol_ratio:.1f}x大涨{change_pct:.1f}%，买盘衰竭顶部信号",
            "Climax"
        ))
    elif vol_ratio >= Decimal("2.0") and change_pct >= Decimal("5.0"):
        signals.append(WyckoffSignal(
            "买盘高潮", "distribution", -10,
            f"极端放量冲高，警惕主力出货",
            "Climax"
        ))

    # Selling Climax: huge volume + big down move = potential capitulation
    if vol_ratio >= Decimal("2.5") and change_pct <= Decimal("-3.0"):
        signals.append(WyckoffSignal(
            "卖盘高潮", "accumulation", 12,
            f"放量{vol_ratio:.1f}x急跌{change_pct:.1f}%，恐慌抛售衰竭底部信号",
            "Climax"
        ))
    elif vol_ratio >= Decimal("2.0") and change_pct <= Decimal("-5.0"):
        signals.append(WyckoffSignal(
            "卖盘高潮", "accumulation", 10,
            f"极端放量暴跌，恐慌盘出清",
            "Climax"
        ))

    # Moderate climax signals
    if vol_ratio >= Decimal("1.8") and amplitude_pct >= Decimal("5"):
        if change_pct > 0:
            signals.append(WyckoffSignal(
                "放量宽震", "distribution", -6,
                f"放量{vol_ratio:.1f}x宽幅震荡，多空分歧加大",
                "Climax"
            ))
        else:
            signals.append(WyckoffSignal(
                "放量宽震", "accumulation", 6,
                f"放量{vol_ratio:.1f}x宽幅震荡，底部换手",
                "Climax"
            ))

    return signals


def _detect_accumulation(
    rsi14: Decimal,
    vol_ratio: Decimal,
    vol_trend: str,         # "shrinking" | "expanding" | "stable"
    price_vs_ma60: Decimal,  # price / MA60 ratio
    daily_vol_shrinking: bool = False,
) -> list[WyckoffSignal]:
    """检测吸筹特征

    Accumulation Phase 特征:
      1. 价格在MA60附近（±5%）— 支撑区
      2. 成交量持续萎缩 — 抛压衰竭
      3. RSI从超卖区回升 — 多头积蓄力量
      4. 振幅收窄 — 蓄势
    """
    signals = []
    score = 0
    reasons = []

    # Near support (MA60 ± 5%)
    near_support = Decimal("0.95") <= price_vs_ma60 <= Decimal("1.05")
    if near_support:
        score += 4
        reasons.append("价格在MA60支撑区")

    # Volume shrinking = selling pressure fading
    if vol_ratio < Decimal("0.7") and vol_trend == "shrinking":
        score += 5
        reasons.append("缩量至极，抛压衰竭")
    elif vol_ratio < Decimal("0.85") and daily_vol_shrinking:
        score += 3
        reasons.append("连续缩量")

    # RSI recovering from oversold
    if rsi14 <= Decimal("40") and rsi14 >= Decimal("25"):
        score += 4
        reasons.append(f"RSI={rsi14:.0f}从超卖区回升")
    elif rsi14 <= Decimal("30"):
        score += 5
        reasons.append(f"RSI={rsi14:.0f}深度超卖，反弹一触即发")

    # RSI rising trend
    if Decimal("30") <= rsi14 <= Decimal("50"):
        score += 2
        reasons.append("RSI低位回升中")

    if score >= 8:
        signals.append(WyckoffSignal(
            "吸筹特征", "accumulation", min(score, 15),
            f"威科夫吸筹区：{'；'.join(reasons)}",
            "Accumulation"
        ))
    elif score >= 5:
        signals.append(WyckoffSignal(
            "吸筹萌芽", "accumulation", score,
            f"疑似吸筹：{'；'.join(reasons)}",
            "Accumulation"
        ))

    return signals


def _detect_distribution(
    rsi14: Decimal,
    vol_ratio: Decimal,
    vol_trend: str,
    price_vs_ma20: Decimal,   # price / MA20 ratio
    price_vs_ma60: Decimal,
) -> list[WyckoffSignal]:
    """检测派发特征

    Distribution Phase 特征:
      1. 价格远高于MA60（+15%以上）— 获利盘压力
      2. 放量但价格不涨（滞涨）— 主力出货
      3. RSI从超买区回落
      4. 上影线长（盘中冲高回落）
    """
    signals = []
    score = 0
    reasons = []

    # Far above support = profit-taking pressure
    if price_vs_ma60 > Decimal("1.15"):
        score += 5
        reasons.append("远离MA60成本区+15%，获利盘压力大")
    elif price_vs_ma60 > Decimal("1.08"):
        score += 2
        reasons.append("偏离MA60较远")

    # Volume expanding but price stalling = distribution
    if vol_ratio > Decimal("1.3") and vol_trend == "expanding":
        # Check if price is not rallying proportionally
        if price_vs_ma20 < Decimal("1.02"):
            score += 6
            reasons.append("放量滞涨，典型派发特征")

    # RSI overbought reversing
    if rsi14 >= Decimal("70"):
        score += 5
        reasons.append(f"RSI={rsi14:.0f}超买区，回调风险")
    elif rsi14 >= Decimal("60"):
        score += 2
        reasons.append("RSI高位")

    if score >= 7:
        signals.append(WyckoffSignal(
            "派发特征", "distribution", -min(score, 15),
            f"威科夫派发区：{'；'.join(reasons)}",
            "Distribution"
        ))

    return signals


def _detect_spring(
    daily_lows: list[Decimal],
    daily_closes: list[Decimal],
    ma60: Decimal,
    recovery_pct: Decimal,
) -> list[WyckoffSignal]:
    """检测弹簧形态 — 假跌破支撑后迅速收回

    Spring Pattern:
      1. 日内最低价跌破MA60支撑
      2. 收盘价收回MA60上方
      3. 收回幅度>1%（不是偶然的刺穿）
    → 极强买入信号，说明主力在护盘
    """
    signals = []
    if len(daily_lows) < 2 or ma60 <= 0:
        return signals

    # Check: today's low broke below MA60, but close is above
    today_low = daily_lows[-1]
    today_close = daily_closes[-1] if daily_closes else Decimal("0")

    if today_low < ma60 and today_close > ma60:
        # Recovery amount
        recovery = (today_close - today_low) / today_low * 100 if today_low > 0 else Decimal("0")
        if recovery >= Decimal("2.0"):
            signals.append(WyckoffSignal(
                "弹簧信号", "accumulation", 14,
                f"威科夫Spring：盘中跌破MA60{ma60:.2f}后强势收回+{recovery:.1f}%，假跌破确认",
                "Spring"
            ))
        elif recovery >= Decimal("1.0"):
            signals.append(WyckoffSignal(
                "弹簧萌芽", "accumulation", 8,
                f"疑似Spring：跌破MA60后收回+{recovery:.1f}%，关注确认",
                "Spring"
            ))

    # Also check previous day for spring then follow-through
    if len(daily_lows) >= 2:
        prev_low = daily_lows[-2]
        prev_close = daily_closes[-2]
        if prev_low < ma60 and prev_close > ma60:
            recovery = (prev_close - prev_low) / prev_low * 100 if prev_low > 0 else Decimal("0")
            if recovery >= Decimal("2.0") and today_close > prev_close:
                signals.append(WyckoffSignal(
                    "弹簧确认", "accumulation", 10,
                    f"Spring次日继续上涨：弹簧信号确认有效",
                    "Spring"
                ))

    return signals


# ═══════════════════════════════════════════════════════════════
# Unified Entry Point
# ═══════════════════════════════════════════════════════════════

def analyze_wyckoff(
    change_pct: Decimal,
    vol_ratio: Decimal,
    amplitude_pct: Decimal,
    rsi14: Decimal,
    price_vs_ma20: Decimal,
    price_vs_ma60: Decimal,
    *,
    vol_trend: str = "stable",
    daily_vol_shrinking: bool = False,
    daily_lows: list[Decimal] | None = None,
    daily_closes: list[Decimal] | None = None,
    ma60: Decimal = Decimal("0"),
) -> tuple[int, str, list[WyckoffSignal]]:
    """统一入口：分析威科夫量价结构并返回净分数调整。

    Args:
        change_pct: 当日涨跌幅%
        vol_ratio: 量比（当前量/5日均量）
        amplitude_pct: 日内振幅%
        rsi14: 14日RSI
        price_vs_ma20: 现价/MA20比值
        price_vs_ma60: 现价/MA60比值
        vol_trend: 量能趋势 "shrinking"/"expanding"/"stable"
        daily_vol_shrinking: 日线级别是否连续缩量
        daily_lows: 日线最低价列表
        daily_closes: 日线收盘价列表
        ma60: MA60值

    Returns:
        score_adjustment: 净分数调整 (-20 to +20)
        summary: 可读摘要
        signals: 所有信号列表
    """
    all_signals: list[WyckoffSignal] = []

    # 1. Climax Detection (买卖高潮) — highest priority
    climax = _detect_climax(change_pct, vol_ratio, amplitude_pct)
    all_signals.extend(climax)

    # 2. Spring Detection (弹簧) — high priority buy signal
    if daily_lows and daily_closes:
        spring = _detect_spring(daily_lows, daily_closes, ma60, Decimal("0"))
        all_signals.extend(spring)

    # 3. Accumulation Detection (吸筹)
    accum = _detect_accumulation(rsi14, vol_ratio, vol_trend, price_vs_ma60, daily_vol_shrinking)
    all_signals.extend(accum)

    # 4. Distribution Detection (派发)
    dist = _detect_distribution(rsi14, vol_ratio, vol_trend, price_vs_ma20, price_vs_ma60)
    all_signals.extend(dist)

    # Aggregate
    net_score = sum(s.strength for s in all_signals)
    net_score = max(-20, min(20, net_score))  # Cap at ±20 to complement UZI's ±30

    # Build summary
    acc_signals = [s for s in all_signals if s.direction == "accumulation"]
    dist_signals = [s for s in all_signals if s.direction == "distribution"]

    summary_parts = []
    if acc_signals:
        summary_parts.append(f"威科夫看多: {'; '.join(s.description for s in acc_signals[:2])}")
    if dist_signals:
        summary_parts.append(f"威科夫看空: {'; '.join(s.description for s in dist_signals[:2])}")

    summary = " | ".join(summary_parts) if summary_parts else "威科夫: 无显著信号"

    return net_score, summary, all_signals
