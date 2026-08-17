#!/usr/bin/env python3
"""资金流向模块 — 北向资金 + 个股财务指标。

数据源：
  - 北向资金：akshare stock_hsgt_fund_flow_summary_em（东方财富）
  - 个股财务：腾讯 qt.gtimg.cn 内嵌 PE/PB/市值/量比/换手率

对评分引擎的影响：
  - get_northbound_signal() → direction/strength，供 analysis.py 引用
  - 北向大幅流出 → 全局风控收紧，降低 buy_score 置信度
  - 北向大幅流入 → 适度放宽，提高 hold_score 容错

Usage:
  python3 -B stock_advisor/fund_flow.py
"""

from __future__ import annotations

import logging
import subprocess
from datetime import date
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 北向资金（akshare）
# ═══════════════════════════════════════════════════════════════

_NB_CACHE: dict = {"ts": 0, "data": {}}


def get_northbound_flow(force_refresh: bool = False) -> dict:
    """获取北向资金日度净流入（沪深股通）。

    Returns:
        {
            "hk2sh": {"net_inflow_yi": Decimal, "direction": "北向"},
            "hk2sz": {"net_inflow_yi": Decimal, "direction": "北向"},
            "hk_total_net_yi": Decimal,    # 沪深股通合计净流入(亿)
            "south_net_yi": Decimal,        # 南向合计(亿) 港股通
            "updated": "2026-05-28",
            "error": None,                  # 有错误时非空
        }
    """
    import time
    global _NB_CACHE

    # 5分钟缓存
    if not force_refresh and _NB_CACHE["ts"] and (time.time() - _NB_CACHE["ts"]) < 300:
        return _NB_CACHE["data"]

    try:
        import akshare as ak
        df = ak.stock_hsgt_fund_flow_summary_em()
    except Exception as e:
        logger.warning(f"akshare 北向资金获取失败: {e}")
        return _build_empty_nb(error=str(e)[:100])

    if df is None or df.empty:
        return _build_empty_nb(error="北向资金数据为空")

    out = _build_empty_nb()
    try:
        # 筛选最新交易日
        latest_date = df["交易日"].max()
        today_df = df[df["交易日"] == latest_date]

        # 北向：沪股通
        hk2sh_rows = today_df[(today_df["板块"] == "沪股通") & (today_df["资金方向"] == "北向")]
        if not hk2sh_rows.empty:
            row = hk2sh_rows.iloc[0]
            out["hk2sh"] = {
                "net_inflow_yi": _safe_dec(row.get("资金净流入", 0)) / Decimal("100000000"),
                "buy_amount_yi": _safe_dec(row.get("成交净买额", 0)) / Decimal("100000000"),
                "direction": "北向",
            }

        # 北向：深股通
        hk2sz_rows = today_df[(today_df["板块"] == "深股通") & (today_df["资金方向"] == "北向")]
        if not hk2sz_rows.empty:
            row = hk2sz_rows.iloc[0]
            out["hk2sz"] = {
                "net_inflow_yi": _safe_dec(row.get("资金净流入", 0)) / Decimal("100000000"),
                "buy_amount_yi": _safe_dec(row.get("成交净买额", 0)) / Decimal("100000000"),
                "direction": "北向",
            }

        out["hk_total_net_yi"] = out["hk2sh"]["net_inflow_yi"] + out["hk2sz"]["net_inflow_yi"]

        # 南向合计
        south_rows = today_df[today_df["资金方向"] == "南向"]
        total_south = Decimal("0")
        for _, row in south_rows.iterrows():
            total_south += _safe_dec(row.get("资金净流入", 0))
        out["south_net_yi"] = total_south / Decimal("100000000")

        out["updated"] = str(latest_date)
        out["error"] = None

    except Exception as e:
        logger.warning(f"北向资金解析失败: {e}")
        out["error"] = str(e)[:100]

    _NB_CACHE = {"ts": time.time(), "data": out}
    return out


def _build_empty_nb(error: Optional[str] = None) -> dict:
    return {
        "hk2sh": {"net_inflow_yi": Decimal("0"), "buy_amount_yi": Decimal("0"), "direction": "北向"},
        "hk2sz": {"net_inflow_yi": Decimal("0"), "buy_amount_yi": Decimal("0"), "direction": "北向"},
        "hk_total_net_yi": Decimal("0"),
        "south_net_yi": Decimal("0"),
        "updated": str(date.today()),
        "error": error,
    }


def _safe_dec(val) -> Decimal:
    """安全转 Decimal，处理 NaN/None。"""
    import math
    try:
        v = float(val)
        if math.isnan(v) or math.isinf(v):
            return Decimal("0")
        return Decimal(str(v))
    except (ValueError, TypeError):
        return Decimal("0")


# ═══════════════════════════════════════════════════════════════
# 评分引擎信号
# ═══════════════════════════════════════════════════════════════

def get_northbound_signal() -> dict:
    """给评分引擎用的北向资金信号。

    Returns:
        {"direction": "inflow"|"outflow"|"flat",
         "net_yi": Decimal, "strength": "strong"|"normal"|"weak",
         "bias": Decimal}  # -1.0 ~ +1.0 多头/空头偏向
    """
    nb = get_northbound_flow()
    if nb.get("error"):
        return {"direction": "flat", "net_yi": Decimal("0"), "strength": "weak", "bias": Decimal("0")}

    net = nb["hk_total_net_yi"]
    if net > Decimal("50"):
        direction, strength, bias = "inflow", "strong", Decimal("0.8")
    elif net > Decimal("15"):
        direction, strength, bias = "inflow", "normal", Decimal("0.4")
    elif net > Decimal("5"):
        direction, strength, bias = "inflow", "weak", Decimal("0.15")
    elif net < Decimal("-50"):
        direction, strength, bias = "outflow", "strong", Decimal("-0.8")
    elif net < Decimal("-15"):
        direction, strength, bias = "outflow", "normal", Decimal("-0.4")
    elif net < Decimal("-5"):
        direction, strength, bias = "outflow", "weak", Decimal("-0.15")
    else:
        direction, strength, bias = "flat", "weak", Decimal("0")

    return {"direction": direction, "net_yi": net, "strength": strength, "bias": bias}


# ═══════════════════════════════════════════════════════════════
# 个股财务指标（腾讯行情）
# ═══════════════════════════════════════════════════════════════

TENCENT_QUOTE_URL = "http://qt.gtimg.cn/q={symbols}"


def get_stock_financials(codes: list[str]) -> dict[str, dict]:
    """从腾讯行情批量提取财务指标。

    Args:
        codes: 股票代码列表，如 ["sh601698", "sz000063"]

    Returns:
        {code: {pe, pb, market_cap_yi, circ_cap_yi, volume_ratio, turnover_rate, amplitude}}
    """
    if not codes:
        return {}

    symbols = ",".join(codes)
    url = TENCENT_QUOTE_URL.format(symbols=symbols)

    from .platform_compat import http_get_text
    raw = http_get_text(url, timeout=15, encoding="gbk")
    if not raw:
        logger.warning("腾讯行情获取失败")
        return {}

    out = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        try:
            _, payload = line.split("=", 1)
        except ValueError:
            continue
        payload = payload.strip('\";\n ')
        parts = payload.split("~")
        if len(parts) < 50:
            continue

        try:
            code = parts[2]
            out[code] = {
                "pe": _parse_dec(parts[39]) if len(parts) > 39 else Decimal("0"),
                "pb": _parse_dec(parts[46]) if len(parts) > 46 else Decimal("0"),
                "market_cap_yi": _parse_dec(parts[44]) if len(parts) > 44 else Decimal("0"),
                "circ_cap_yi": _parse_dec(parts[45]) if len(parts) > 45 else Decimal("0"),
                "volume_ratio": _parse_dec(parts[49]) if len(parts) > 49 else Decimal("0"),
                "turnover_rate": _parse_dec(parts[38]) if len(parts) > 38 else Decimal("0"),
                "amplitude": _parse_dec(parts[43]) if len(parts) > 43 else Decimal("0"),
            }
        except Exception:
            continue

    return out


def _parse_dec(val: str) -> Decimal:
    val = (val or "").strip()
    if not val:
        return Decimal("0")
    try:
        return Decimal(val)
    except Exception:
        return Decimal("0")


# ═══════════════════════════════════════════════════════════════
# 资金面摘要（供 CLI / 简报 / 复盘 使用）
# ═══════════════════════════════════════════════════════════════

def format_fund_flow_md(portfolio_codes: Optional[list[str]] = None, realtime: bool = False) -> str:
    """生成资金面摘要 markdown。

    Args:
        portfolio_codes: 持仓股票代码（如 ["601698", ...]），为空则只输出北向资金
        realtime: True=盘中实时分钟级资金流，False=日级别
    """
    lines = []

    # 1. 北向资金
    nb = get_northbound_flow()
    if nb.get("error"):
        lines.append(f"**北向资金**：数据暂不可用")
    else:
        hk_total = nb["hk_total_net_yi"]
        if hk_total > 0:
            direction = f"🟢 **净流入 {hk_total:.1f}亿**"
        elif hk_total < 0:
            direction = f"🔴 **净流出 {abs(hk_total):.1f}亿**"
        else:
            direction = "⚪ 持平"

        sh = nb["hk2sh"]["net_inflow_yi"]
        sz = nb["hk2sz"]["net_inflow_yi"]
        south = nb.get("south_net_yi", Decimal("0"))

        lines.append(f"**北向资金**：{direction}")
        lines.append(f"  沪股通 {sh:+.1f}亿 | 深股通 {sz:+.1f}亿")
        if south != 0:
            lines.append(f"  南向(港股通) {south:+.1f}亿")

    # 2. 持仓资金流
    if portfolio_codes:
        try:
            from .chrome_scraper import get_multi_fund_flow, get_stock_fund_flow_realtime
            if realtime:
                lines.append("")
                lines.append("**盘中实时资金流**：")
                for code in portfolio_codes:
                    rt = get_stock_fund_flow_realtime(code)
                    if rt:
                        deltas = " → ".join(
                            f"{m['time']} {m['delta_yi']:+.2f}亿" if m['delta_yi'] != 0 else f"{m['time']} -"
                            for m in rt['minutes']
                        )
                        lines.append(
                            f"  {code} {rt['name']} {rt['direction']} "
                            f"累计{rt['cumulative_yi']:+.2f}亿"
                        )
                        lines.append(f"    近5分钟：{deltas}")
            else:
                ff_data = get_multi_fund_flow(portfolio_codes)
                if ff_data:
                    lines.append("")
                    lines.append("**个股主力资金**：")
                    for code in portfolio_codes:
                        ff = ff_data.get(code)
                        if ff:
                            d = "🟢" if ff["main_net_yi"] > 0 else ("🔴" if ff["main_net_yi"] < 0 else "⚪")
                            lines.append(f"  {code} {ff['name']} {d} {ff['main_net_yi']:+.2f}亿")
        except Exception:
            pass  # Chrome CDP 不可用时静默跳过

    # 3. 持仓财务
    if portfolio_codes:
        symbols = []
        for c in portfolio_codes:
            prefix = "sh" if c.startswith(("6", "9")) else "sz"
            symbols.append(f"{prefix}{c}")

        fin = get_stock_financials(symbols)
        if fin:
            lines.append("")
            lines.append("**持仓财务**：")
            for code in portfolio_codes:
                f = fin.get(code)
                if f:
                    pe_str = f"{f['pe']:.0f}" if f["pe"] > 0 else "亏损"
                    lines.append(
                        f"  {code} PE {pe_str} | PB {f['pb']:.1f} | "
                        f"市值 {f['market_cap_yi']:.0f}亿 | "
                        f"量比 {f['volume_ratio']:.2f} | 换手 {f['turnover_rate']:.1f}%"
                    )

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=== 北向资金 ===")
    import json as _json
    nb = get_northbound_flow(force_refresh=True)
    print(_json.dumps(nb, ensure_ascii=False, default=str, indent=2))
    print()

    print("=== 个股财务 ===")
    fin = get_stock_financials(["sh601698", "sz000063", "sz002439"])
    for code, f in fin.items():
        pe_s = f"{f['pe']:.0f}" if f["pe"] > 0 else "亏损"
        print(f"{code}: PE={pe_s} PB={f['pb']:.1f} 市值={f['market_cap_yi']:.0f}亿 量比={f['volume_ratio']:.2f}")
    print()

    print("=== 评分信号 ===")
    sig = get_northbound_signal()
    print(f"direction={sig['direction']} net={sig['net_yi']}亿 strength={sig['strength']} bias={sig['bias']}")
    print()

    print(format_fund_flow_md(["601698", "000063", "002439"]))
