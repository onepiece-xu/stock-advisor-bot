"""
UZI-Skill 启发的多维规则增强 — 无LLM延迟

来源：UZI-Skill (⭐1612) — 22维数据 × 180条量化规则 × 17种分析方法

新增维度（叠加在现有三层评分之上）：
  1. 量价背离检测 — 价格涨但缩量 = 假突破
  2. 相对强弱 — 个股vs大盘/板块
  3. 连续方向 — 多根K线同向 = 趋势强化
  4. 波动率异动 — 突然放大的振幅 = 变盘前兆
  5. 缺口引力 — 未回补缺口作为支撑/压力
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


@dataclass
class UziSignal:
    """UZI风格信号"""
    dimension: str           # 维度名
    direction: Literal["bullish", "bearish", "neutral"]
    strength: int            # -10到+10
    description: str         # 人类可读描述


def _price_volume_divergence(
    current_price: Decimal,
    current_volume: Decimal,
    avg_volume_5: Decimal,
    avg_volume_20: Decimal,
    recent_bars: list[dict],
) -> list[UziSignal]:
    """量价背离检测
    - 价涨量缩 → 上涨乏力 (bearish)
    - 价跌量缩 → 抛压减轻 (bullish)
    - 价涨量增 → 健康上涨 (bullish)
    - 价跌量增 → 恐慌抛售 (bearish)
    """
    signals = []
    if not recent_bars or len(recent_bars) < 3:
        return signals

    # Check last 3 bars for direction
    last_3 = recent_bars[-3:]
    price_changes = []
    vol_changes = []
    for i in range(1, len(last_3)):
        p_change = last_3[i]["close"] - last_3[i - 1]["close"]
        v_change = last_3[i]["volume"] - last_3[i - 1]["volume"]
        price_changes.append(p_change)
        vol_changes.append(v_change)

    price_up = sum(1 for p in price_changes if p > 0)
    vol_up = sum(1 for v in vol_changes if v > 0)

    # Volume relative to average
    vol_ratio = current_volume / avg_volume_5 if avg_volume_5 > 0 else Decimal("1")

    # Bullish divergence: price falling but volume shrinking = selling pressure fading
    if price_up == 0 and vol_up == 0 and vol_ratio < Decimal("0.7"):
        signals.append(UziSignal("量价背离", "bullish", 8, "价跌量缩，抛压衰竭"))

    # Bearish divergence: price rising but volume shrinking = rally losing steam
    if price_up == 2 and vol_up == 0 and vol_ratio < Decimal("0.8"):
        signals.append(UziSignal("量价背离", "bearish", -8, "价涨量缩，上涨乏力"))

    # Healthy rally: price + volume both up
    if price_up >= 1 and vol_up >= 1 and vol_ratio > Decimal("1.2"):
        signals.append(UziSignal("量价配合", "bullish", 6, "放量上涨，量价健康"))

    # Panic selling: price down + volume up
    if price_up == 0 and vol_up >= 1 and vol_ratio > Decimal("1.5"):
        signals.append(UziSignal("量价背离", "bearish", -10, "放量下跌，恐慌抛售"))

    return signals


def _relative_strength(
    stock_change: Decimal,
    index_change: Decimal,
    sector_change: Decimal | None,
    consecutive_days: int = 1,
) -> list[UziSignal]:
    """相对强弱分析
    - 个股涨 > 大盘涨 = 强势股
    - 个股跌 < 大盘跌 = 抗跌股
    - 个股涨 < 大盘涨 = 跟涨（弱）
    - 个股跌 > 大盘跌 = 领跌（弱）
    """
    signals = []

    rel_vs_index = stock_change - index_change

    if rel_vs_index > Decimal("2"):
        signals.append(UziSignal("相对强弱", "bullish", 7, f"跑赢大盘{rel_vs_index:+.1f}%，强势股特征"))
    elif rel_vs_index > Decimal("0.5"):
        signals.append(UziSignal("相对强弱", "bullish", 3, f"略强于大盘{rel_vs_index:+.1f}%"))
    elif rel_vs_index < Decimal("-2"):
        signals.append(UziSignal("相对强弱", "bearish", -7, f"跑输大盘{rel_vs_index:+.1f}%，弱势股"))
    elif rel_vs_index < Decimal("-0.5"):
        signals.append(UziSignal("相对强弱", "bearish", -2, f"略弱于大盘{rel_vs_index:+.1f}%"))

    if sector_change is not None:
        rel_vs_sector = stock_change - sector_change
        if rel_vs_sector > Decimal("1.5"):
            signals.append(UziSignal("板块强度", "bullish", 5, f"领涨板块，龙头特征"))
        elif rel_vs_sector < Decimal("-1.5"):
            signals.append(UziSignal("板块强度", "bearish", -5, f"拖累板块，后排跟风"))

    # Consecutive relative strength
    if consecutive_days >= 3 and rel_vs_index > 0:
        signals.append(UziSignal("持续强势", "bullish", 8, f"连续{consecutive_days}日跑赢大盘"))
    elif consecutive_days >= 3 and rel_vs_index < 0:
        signals.append(UziSignal("持续弱势", "bearish", -8, f"连续{consecutive_days}日跑输大盘"))

    return signals


def _volatility_signal(
    current_amplitude: Decimal,
    avg_amplitude_10: Decimal,
    current_volume: Decimal,
    avg_volume_5: Decimal,
) -> list[UziSignal]:
    """波动率异动检测"""
    signals = []

    amp_ratio = current_amplitude / avg_amplitude_10 if avg_amplitude_10 > 0 else Decimal("1")
    vol_ratio = current_volume / avg_volume_5 if avg_volume_5 > 0 else Decimal("1")

    # Sudden amplitude expansion = regime change signal
    if amp_ratio > Decimal("2") and vol_ratio > Decimal("1.5"):
        signals.append(UziSignal("波动率异动", "neutral", 0, f"振幅突增至{amp_ratio:.1f}倍+放量，变盘信号"))
    elif amp_ratio > Decimal("3"):
        signals.append(UziSignal("波动率异动", "neutral", 0, f"极端振幅{amp_ratio:.1f}倍，高风险"))

    # Shrinking volatility + shrinking volume = coiling spring
    if amp_ratio < Decimal("0.5") and vol_ratio < Decimal("0.6"):
        signals.append(UziSignal("波动压缩", "neutral", 0, "振幅缩量至极，蓄势待发"))

    return signals


def _consecutive_direction(
    recent_bars: list[dict],
    lookback: int = 5,
) -> list[UziSignal]:
    """连续方向检测 — 多根K线同向强化趋势信号"""
    signals = []
    if len(recent_bars) < lookback:
        return signals

    closes = [b["close"] for b in recent_bars[-lookback:]]
    up_count = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
    down_count = sum(1 for i in range(1, len(closes)) if closes[i] < closes[i - 1])

    # Strong consecutive trend
    total = len(closes) - 1
    if up_count == total:
        signals.append(UziSignal("连续方向", "bullish", 8, f"连续{total}根阳线，多头强攻"))
    elif down_count == total:
        signals.append(UziSignal("连续方向", "bearish", -8, f"连续{total}根阴线，空头压制"))
    elif up_count >= total - 1:
        signals.append(UziSignal("连续方向", "bullish", 4, f"{up_count}/{total}根阳线，偏多"))
    elif down_count >= total - 1:
        signals.append(UziSignal("连续方向", "bearish", -4, f"{down_count}/{total}根阴线，偏空"))

    return signals


def analyze_uzi_signals(
    current_price: Decimal,
    current_volume: Decimal,
    current_change: Decimal,
    current_amplitude: Decimal,
    avg_volume_5: Decimal,
    avg_volume_20: Decimal,
    avg_amplitude_10: Decimal,
    index_change: Decimal,
    sector_change: Decimal | None,
    recent_minute_bars: list[dict],
    *,
    consecutive_strong_days: int = 0,
) -> tuple[int, str, list[UziSignal]]:
    """Unified entry: analyze all UZI dimensions and return net score adjustment.

    Returns:
        score_adjustment: net score change (-30 to +30)
        summary: human-readable summary
        signals: all individual signals for logging
    """
    all_signals: list[UziSignal] = []

    # 1. Volume-Price Analysis
    vp = _price_volume_divergence(current_price, current_volume, avg_volume_5, avg_volume_20, recent_minute_bars)
    all_signals.extend(vp)

    # 2. Relative Strength
    rs = _relative_strength(current_change, index_change, sector_change, consecutive_strong_days)
    all_signals.extend(rs)

    # 3. Volatility
    vol = _volatility_signal(current_amplitude, avg_amplitude_10, current_volume, avg_volume_5)
    all_signals.extend(vol)

    # 4. Consecutive Direction
    cd = _consecutive_direction(recent_minute_bars, lookback=5)
    all_signals.extend(cd)

    # Aggregate score
    net_score = sum(s.strength for s in all_signals)
    # Cap to prevent over-influence
    net_score = max(-30, min(30, net_score))

    bullish = [s for s in all_signals if s.direction == "bullish"]
    bearish = [s for s in all_signals if s.direction == "bearish"]

    summary_parts = []
    if bullish:
        summary_parts.append(f"UZI看多: {'; '.join(s.description for s in bullish[:3])}")
    if bearish:
        summary_parts.append(f"UZI看空: {'; '.join(s.description for s in bearish[:3])}")

    summary = " | ".join(summary_parts) if summary_parts else "UZI: 无显著信号"

    return net_score, summary, all_signals
