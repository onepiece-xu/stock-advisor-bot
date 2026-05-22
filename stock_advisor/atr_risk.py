"""
ATR 动态止损 — Average True Range based risk management

灵感来源：TA-Lib + pyfolio 的组合风险管理

核心理念：
  - 固定 -7% 止损忽略了个股波动差异
  - 波动大的股（如卫通）正常回撤就可能触发 -7%
  - 波动小的股（如银行）跌 -3% 已经是异常
  - ATR 根据个股实际波动自动调整止损距离

公式：
  True Range = max(H-L, |H-PrevClose|, |L-PrevClose|)
  ATR = Wilder's smoothed average of TR over N periods
  止损价 = 入场价 - ATR × multiplier
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

import requests

logger = logging.getLogger(__name__)

# Default multipliers
ATR_STOP_MULTIPLIER = Decimal("2.0")    # Normal stop: entry - 2×ATR
ATR_WIDE_MULTIPLIER = Decimal("3.0")     # Wide stop for volatile stocks


@dataclass
class ATRResult:
    """ATR calculation result"""
    atr: Decimal               # Current ATR value
    atr_pct: Decimal            # ATR as % of current price
    daily_tr: list[Decimal]     # Recent True Ranges (for debugging)
    volatility_level: Literal["low", "normal", "high", "extreme"]
    dynamic_stop: Decimal       # Recommended stop-loss price
    dynamic_stop_pct: Decimal   # Stop distance as %


def _fetch_daily_ohlc(symbol: str, ndays: int = 20) -> tuple[list[Decimal], list[Decimal], list[Decimal]] | None:
    """Fetch daily OHLC data from East Money.
    
    Returns (highs, lows, closes) or None on failure.
    """
    # Determine market code
    if symbol.startswith("sh"):
        market = "1"
    elif symbol.startswith("sz"):
        market = "0"
    else:
        return None
    
    code = symbol[2:]  # Strip sh/sz prefix
    
    today = datetime.now().date()
    lookback = today - timedelta(days=ndays * 3)
    
    try:
        resp = requests.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": f"{market}.{code}",
                "klt": "101",          # Daily K-line
                "fqt": "1",            # Forward-adjusted
                "lmt": str(ndays + 5),
                "beg": lookback.strftime("%Y%m%d"),
                "end": today.strftime("%Y%m%d"),
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
                # fields: date, open, close, high, low, volume
                closes.append(Decimal(fields[2]))
                highs.append(Decimal(fields[3]))
                lows.append(Decimal(fields[4]))
        
        if len(closes) < 5:
            return None
        
        return highs[-ndays:], lows[-ndays:], closes[-ndays:]
    
    except Exception as exc:
        logger.warning("ATR: Failed to fetch OHLC for %s: %s", symbol, exc)
        return None


def compute_true_ranges(highs: list[Decimal], lows: list[Decimal], closes: list[Decimal]) -> list[Decimal]:
    """Compute True Range series.
    
    TR = max(H-L, |H-PrevClose|, |L-PrevClose|)
    """
    if len(highs) < 2:
        return []
    
    trs = []
    for i in range(1, len(highs)):
        h_l = highs[i] - lows[i]
        h_pc = abs(highs[i] - closes[i-1])
        l_pc = abs(lows[i] - closes[i-1])
        tr = max(h_l, h_pc, l_pc)
        trs.append(tr)
    
    return trs


def compute_atr(highs: list[Decimal], lows: list[Decimal], closes: list[Decimal], period: int = 14) -> Decimal | None:
    """Compute ATR using Wilder's smoothing method.
    
    ATR = (Previous ATR × (period-1) + Current TR) / period
    """
    trs = compute_true_ranges(highs, lows, closes)
    if len(trs) < period:
        return None
    
    # Initial ATR = simple average of first 'period' TRs
    atr = sum(trs[:period], Decimal("0")) / Decimal(str(period))
    
    # Wilder's smoothing for remaining periods
    for i in range(period, len(trs)):
        atr = (atr * Decimal(str(period - 1)) + trs[i]) / Decimal(str(period))
    
    return atr.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def compute_atr_stop(
    symbol: str,
    current_price: Decimal,
    entry_price: Decimal | None = None,
    *,
    multiplier: Decimal | None = None,
) -> ATRResult | None:
    """Compute dynamic stop-loss price based on ATR.
    
    Args:
        symbol: Stock symbol (sh601698 / sz000063)
        current_price: Current market price
        entry_price: Entry/cost price. If None, uses current_price.
        multiplier: ATR multiplier for stop distance (default: 2.0)
    
    Returns:
        ATRResult with dynamic stop price, or None if data fetch fails.
    """
    if multiplier is None:
        multiplier = ATR_STOP_MULTIPLIER
    
    ohlc = _fetch_daily_ohlc(symbol, ndays=20)
    if ohlc is None:
        return None
    
    highs, lows, closes = ohlc
    if len(closes) < 15:
        return None
    
    atr = compute_atr(highs, lows, closes)
    if atr is None or atr <= 0:
        return None
    
    # ATR as percentage of current price
    atr_pct = (atr / current_price * 100).quantize(Decimal("0.01"))
    
    # Volatility classification
    if atr_pct >= Decimal("5"):
        vol = "extreme"
        # Use wider multiplier for extreme volatility
        effective_mult = max(multiplier, ATR_WIDE_MULTIPLIER)
    elif atr_pct >= Decimal("3"):
        vol = "high"
        effective_mult = max(multiplier, Decimal("2.5"))
    elif atr_pct >= Decimal("1.5"):
        vol = "normal"
        effective_mult = multiplier
    else:
        vol = "low"
        # Low volatility: tighter stop
        effective_mult = min(multiplier, Decimal("1.5"))
    
    # Dynamic stop-loss
    ref_price = entry_price if entry_price and entry_price > 0 else current_price
    stop_distance = atr * effective_mult
    dynamic_stop = (ref_price - stop_distance).quantize(Decimal("0.01"))
    dynamic_stop_pct = (stop_distance / ref_price * 100).quantize(Decimal("0.01"))
    
    return ATRResult(
        atr=atr,
        atr_pct=atr_pct,
        daily_tr=[],  # Omit for brevity
        volatility_level=vol,
        dynamic_stop=dynamic_stop,
        dynamic_stop_pct=dynamic_stop_pct,
    )


def get_atr_stop_description(result: ATRResult) -> str:
    """Generate human-readable stop-loss description."""
    vol_labels = {"low": "低波动", "normal": "正常波动", "high": "高波动", "extreme": "极端波动"}
    vol_label = vol_labels.get(result.volatility_level, "未知")
    
    lines = [
        f"ATR={result.atr:.2f}（日均波幅{result.atr_pct:.1f}%，{vol_label}）",
        f"动态止损: {result.dynamic_stop:.2f}（-{result.dynamic_stop_pct:.1f}%）",
    ]
    
    if result.volatility_level in ("high", "extreme"):
        lines.append("⚠️ 高波动股票，止损位自动放宽，容忍更大的正常回撤")
    elif result.volatility_level == "low":
        lines.append("低波动股票，止损位较近，跌破即走")
    
    return " | ".join(lines)
