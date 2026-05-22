"""
持仓相关性检测 — Portfolio Correlation Guard

灵感来源：pyfolio (Quantopian) 组合风险分析

核心理念：
  - 如果三只持仓高度同涨同跌，等于买了一只看三只
  - 真正的分散化要求持仓之间相关性低
  - 检测并预警持仓集中风险

方法：
  - 拉取所有持仓的日线收益率序列
  - 计算 pairwise Pearson 相关系数
  - 平均相关性 > 0.7 → 集中度警告
  - 单对相关性 > 0.85 → 强相关警告
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import requests

logger = logging.getLogger(__name__)


@dataclass
class CorrelationPair:
    """单对持仓的相关性"""
    stock_a: str
    stock_b: str
    correlation: float       # -1.0 to 1.0
    strength: str             # "强正相关" / "中度正相关" / "弱相关" / "负相关"


@dataclass
class CorrelationReport:
    """组合相关性分析报告"""
    avg_correlation: float
    max_correlation: float
    pairs: list[CorrelationPair]
    is_concentrated: bool     # True if risk is concentrated
    risk_level: str            # "分散" / "中等集中" / "高度集中"
    warning: str               # Human-readable warning


def _fetch_daily_closes(symbol: str, ndays: int = 30) -> list[Decimal] | None:
    """Fetch daily closing prices for a stock."""
    if symbol.startswith("sh"):
        market = "1"
    elif symbol.startswith("sz"):
        market = "0"
    else:
        return None
    
    code = symbol[2:]
    today = datetime.now().date()
    lookback = today - timedelta(days=ndays * 3)
    
    try:
        resp = requests.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": f"{market}.{code}",
                "klt": "101",
                "fqt": "1",
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
        
        closes = []
        for line in data.get("klines") or []:
            fields = str(line).split(",")
            if len(fields) >= 6:
                closes.append(Decimal(fields[2]))
        
        return closes[-ndays:] if len(closes) >= 10 else None
    
    except Exception as exc:
        logger.warning("Correlation: Failed to fetch closes for %s: %s", symbol, exc)
        return None


def _daily_returns(closes: list[Decimal]) -> list[float]:
    """Compute daily return series."""
    if len(closes) < 2:
        return []
    returns = []
    for i in range(1, len(closes)):
        if closes[i-1] > 0:
            ret = float((closes[i] - closes[i-1]) / closes[i-1])
            returns.append(ret)
    return returns


def _pearson_correlation(x: list[float], y: list[float]) -> float | None:
    """Compute Pearson correlation coefficient."""
    n = min(len(x), len(y))
    if n < 5:
        return None
    
    x = x[-n:]
    y = y[-n:]
    
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    
    cov = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    var_x = sum((xi - mean_x) ** 2 for xi in x)
    var_y = sum((yi - mean_y) ** 2 for yi in y)
    
    if var_x == 0 or var_y == 0:
        return 0.0
    
    return cov / ((var_x * var_y) ** 0.5)


def analyze_portfolio_correlation(
    holdings: list[dict],  # [{"symbol": "sh601698", "name": "中国卫通"}, ...]
    ndays: int = 30,
) -> CorrelationReport | None:
    """Analyze correlation between all portfolio holdings.
    
    Args:
        holdings: List of holding dicts with 'symbol' and 'name' keys
        ndays: Number of trading days for correlation calculation
    
    Returns:
        CorrelationReport or None if insufficient data
    """
    if len(holdings) < 2:
        return None
    
    # Fetch daily closes for all holdings
    returns_map: dict[str, list[float]] = {}
    for h in holdings:
        symbol = h.get("symbol", "")
        closes = _fetch_daily_closes(symbol, ndays=ndays)
        if closes:
            rets = _daily_returns(closes)
            if rets:
                returns_map[h.get("name", symbol)] = rets
    
    if len(returns_map) < 2:
        return None
    
    # Compute pairwise correlations
    names = list(returns_map.keys())
    pairs: list[CorrelationPair] = []
    
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            corr = _pearson_correlation(returns_map[names[i]], returns_map[names[j]])
            if corr is None:
                continue
            
            if corr >= 0.7:
                strength = "强正相关"
            elif corr >= 0.4:
                strength = "中度正相关"
            elif corr >= -0.4:
                strength = "弱相关"
            else:
                strength = "负相关"
            
            pairs.append(CorrelationPair(
                stock_a=names[i],
                stock_b=names[j],
                correlation=round(corr, 3),
                strength=strength,
            ))
    
    if not pairs:
        return None
    
    avg_corr = sum(p.correlation for p in pairs) / len(pairs)
    max_corr = max(p.correlation for p in pairs)
    
    # Risk classification
    if avg_corr >= 0.7:
        is_concentrated = True
        risk_level = "高度集中"
        warning = (f"⚠️ 持仓高度同涨同跌（平均相关性{avg_corr:.1%}），"
                   f"相当于只买了一只股票，分散化无效")
    elif avg_corr >= 0.5:
        is_concentrated = True
        risk_level = "中等集中"
        warning = (f"持仓有一定同向性（平均相关性{avg_corr:.1%}），"
                   f"可考虑增加不同板块标的")
    else:
        is_concentrated = False
        risk_level = "分散"
        warning = f"持仓相关性较低（{avg_corr:.1%}），分散化良好"
    
    return CorrelationReport(
        avg_correlation=round(avg_corr, 3),
        max_correlation=round(max_corr, 3),
        pairs=pairs,
        is_concentrated=is_concentrated,
        risk_level=risk_level,
        warning=warning,
    )


def format_correlation_report(report: CorrelationReport) -> str:
    """Format correlation report for display."""
    lines = [f"📊 持仓相关性分析：{report.risk_level}"]
    lines.append(f"   平均相关性：{report.avg_correlation:.1%}")
    lines.append("")
    
    for p in report.pairs:
        emoji = "🔴" if p.correlation >= 0.7 else "🟡" if p.correlation >= 0.4 else "🟢"
        lines.append(f"   {emoji} {p.stock_a} ↔ {p.stock_b}: {p.correlation:+.3f} ({p.strength})")
    
    lines.append("")
    lines.append(f"   {report.warning}")
    
    return "\n".join(lines)
