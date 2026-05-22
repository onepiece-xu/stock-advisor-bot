"""
多周期确认 — Multi-Timeframe Confirmation

核心理念：
  - 分钟线噪音大，假突破多
  - 周线定大方向，日线定中势，分钟线定买点
  - 三层同向才动手，任何一层反向就减仓/观望

三层架构：
  Layer 1 (周线): 决定能不能买 — Bear → 全面禁买
  Layer 2 (日线): 决定买多少 — 趋势强度影响仓位
  Layer 3 (分钟线): 决定什么价买 — 微调入场点

数据来源：东方财富 K-line API
  日线: klt=101, 周线: klt=102
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

import requests

logger = logging.getLogger(__name__)


@dataclass
class WeeklyRegime:
    """周线 regime 判断结果"""
    regime: Literal["bull", "bear", "neutral"]
    ma5: Decimal
    ma10: Decimal
    current_price: Decimal
    score_adjust: int      # -10 to +10
    description: str


def _fetch_weekly_klines(symbol: str, nweeks: int = 30) -> tuple[list[Decimal], list[Decimal], list[Decimal]] | None:
    """Fetch weekly K-line data from East Money.
    
    Returns (highs, lows, closes) or None on failure.
    """
    if symbol.startswith("sh"):
        market = "1"
    elif symbol.startswith("sz"):
        market = "0"
    else:
        return None
    
    code = symbol[2:]
    
    try:
        resp = requests.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": f"{market}.{code}",
                "klt": "102",          # Weekly K-line
                "fqt": "1",            # Forward-adjusted
                "lmt": str(nweeks + 5),
                "beg": (datetime.now() - timedelta(days=nweeks * 10)).strftime("%Y%m%d"),
                "end": datetime.now().strftime("%Y%m%d"),
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56",
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        data = (resp.json().get("data") or {})
        
        highs: list[Decimal] = []
        lows: list[Decimal] = []
        closes: list[Decimal] = []
        
        for line in data.get("klines") or []:
            fields = str(line).split(",")
            if len(fields) >= 6:
                closes.append(Decimal(fields[2]))
                highs.append(Decimal(fields[3]))
                lows.append(Decimal(fields[4]))
        
        if len(closes) < 10:
            return None
        
        return highs[-nweeks:], lows[-nweeks:], closes[-nweeks:]
    
    except Exception as exc:
        logger.debug("Weekly kline fetch failed for %s: %s", symbol, exc)
        return None


def _simple_ma(values: list[Decimal], period: int) -> Decimal:
    """Simple moving average of last 'period' values."""
    if len(values) < period:
        return Decimal("0")
    window = values[-period:]
    return (sum(window, Decimal("0")) / Decimal(str(period))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def compute_weekly_regime(symbol: str, current_price: Decimal) -> WeeklyRegime | None:
    """Compute weekly timeframe regime for a stock.
    
    Returns WeeklyRegime or None if data fetch fails.
    """
    ohlc = _fetch_weekly_klines(symbol, nweeks=30)
    if ohlc is None:
        return None
    
    highs, lows, closes = ohlc
    
    # Calculate weekly MAs
    weekly_ma5 = _simple_ma(closes, 5)
    weekly_ma10 = _simple_ma(closes, 10)
    
    if weekly_ma5 <= 0 or weekly_ma10 <= 0:
        return None
    
    # Determine regime
    if current_price > weekly_ma5 > weekly_ma10:
        regime = "bull"
        score_adjust = 8
        desc = f"周线多头：价格{current_price:.2f}>MA5({weekly_ma5:.2f})>MA10({weekly_ma10:.2f})"
    elif current_price < weekly_ma5 < weekly_ma10:
        regime = "bear"
        score_adjust = -10
        desc = f"周线空头：价格{current_price:.2f}<MA5({weekly_ma5:.2f})<MA10({weekly_ma10:.2f})"
    else:
        regime = "neutral"
        score_adjust = 0
        desc = "周线震荡，方向不明"
    
    return WeeklyRegime(
        regime=regime,
        ma5=weekly_ma5,
        ma10=weekly_ma10,
        current_price=current_price,
        score_adjust=score_adjust,
        description=desc,
    )


def compute_daily_regime(
    current_price: Decimal,
    daily_ma20: Decimal | None,
    daily_ma60: Decimal | None,
) -> tuple[str, int, str]:
    """Compute daily timeframe regime (extracted from analysis.py logic).
    
    Returns (regime, score_adjust, description).
    This mirrors the existing logic in analysis.py PHASE 0.
    """
    if daily_ma20 and daily_ma60 and daily_ma20 > 0 and daily_ma60 > 0:
        if current_price > daily_ma20 > daily_ma60:
            return ("bull", 5, f"日线多头：价格>{daily_ma20:.2f}>{daily_ma60:.2f}")
        elif current_price < daily_ma20 < daily_ma60:
            return ("bear", -8, f"日线空头：价格<{daily_ma20:.2f}<{daily_ma60:.2f}")
        else:
            return ("neutral", 0, "日线震荡")
    return ("neutral", 0, "日线数据不足")


def multi_timeframe_filter(
    symbol: str,
    current_price: Decimal,
    daily_ma20: Decimal | None,
    daily_ma60: Decimal | None,
) -> dict:
    """Run multi-timeframe analysis and return combined filter result.
    
    Returns dict with:
      - weekly_regime: "bull" | "bear" | "neutral" | None
      - daily_regime: "bull" | "bear" | "neutral"
      - combined_signal: "strong_buy" | "buy" | "caution" | "avoid" | "strong_sell"
      - score_adjust: net score adjustment (-15 to +15)
      - description: human-readable summary
      - block_buy: bool — if True, suppress all buy signals
      - block_sell: bool — if True, suppress all sell signals
    """
    weekly = compute_weekly_regime(symbol, current_price)
    daily_regime, daily_adjust, daily_desc = compute_daily_regime(
        current_price, daily_ma20, daily_ma60
    )
    
    weekly_regime = weekly.regime if weekly else "unknown"
    weekly_adjust = weekly.score_adjust if weekly else 0
    weekly_desc = weekly.description if weekly else "周线数据不可用"
    
    # Combined decision matrix
    if weekly_regime == "bull" and daily_regime == "bull":
        combined = "strong_buy"
        extra_adjust = 5
        desc = f"三周期共振看多 → 满仓做多。{weekly_desc} | {daily_desc}"
        block_buy = False
        block_sell = True  # Don't sell in strong uptrend
    elif weekly_regime == "bull" and daily_regime == "neutral":
        combined = "buy"
        extra_adjust = 3
        desc = f"周多日震 → 回调买入。{weekly_desc} | {daily_desc}"
        block_buy = False
        block_sell = False
    elif weekly_regime == "neutral" and daily_regime == "bull":
        combined = "buy"
        extra_adjust = 2
        desc = f"日线走强 → 可试仓。{weekly_desc} | {daily_desc}"
        block_buy = False
        block_sell = False
    elif weekly_regime == "bear" and daily_regime == "bear":
        combined = "strong_sell"
        extra_adjust = -10
        desc = f"三周期共振看空 → 全面防守。{weekly_desc} | {daily_desc}"
        block_buy = True
        block_sell = False  # Allow selling
    elif weekly_regime == "bear" and daily_regime == "bull":
        combined = "caution"
        extra_adjust = -5
        desc = f"周空日多 → 逆势反弹，勿追。{weekly_desc} | {daily_desc}"
        block_buy = True   # Counter-trend rally: don't buy
        block_sell = False
    elif weekly_regime == "bear" and daily_regime == "neutral":
        combined = "avoid"
        extra_adjust = -5
        desc = f"周空日震 → 观望。{weekly_desc} | {daily_desc}"
        block_buy = True
        block_sell = False
    elif weekly_regime == "bull" and daily_regime == "bear":
        combined = "caution"
        extra_adjust = -3
        desc = f"周多日空 → 等待日线企稳。{weekly_desc} | {daily_desc}"
        block_buy = False   # Buying the dip in weekly uptrend is OK
        block_sell = False
    else:
        combined = "caution"
        extra_adjust = 0
        desc = f"信号混合 → 观望。{weekly_desc} | {daily_desc}"
        block_buy = False
        block_sell = False
    
    total_adjust = weekly_adjust + daily_adjust + extra_adjust
    total_adjust = max(-15, min(15, total_adjust))
    
    return {
        "weekly_regime": weekly_regime,
        "daily_regime": daily_regime,
        "combined_signal": combined,
        "score_adjust": total_adjust,
        "description": desc,
        "block_buy": block_buy,
        "block_sell": block_sell,
        "weekly_detail": weekly_desc,
        "daily_detail": daily_desc,
    }
