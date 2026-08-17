#!/usr/bin/env python3
"""市场宽度模块 — 板块强弱排名 + 市场温度。

数据源：腾讯 qt.gtimg.cn 申万二级行业板块行情（97个板块）
  - 板块涨跌幅排名：97个板块全部可用 ✅
  - 涨跌家数：约16个板块可用（部分板块返回资金流数据），作为参考 ✅
  - 持仓板块上下文：手工映射 ✅

Usage:
  python3 -B stock_advisor/market_breadth.py
"""

from __future__ import annotations

import logging
import subprocess
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# 板块代码（腾讯申万二级行业，扫描确认的97个有效代码）
# ═══════════════════════════════════════════════════════════════

SECTOR_CODES = [
    "pt01801010", "pt01801012", "pt01801014", "pt01801015", "pt01801016",
    "pt01801017", "pt01801018",
    "pt01801030", "pt01801032", "pt01801033", "pt01801034", "pt01801036",
    "pt01801037", "pt01801038", "pt01801039",
    "pt01801040", "pt01801043", "pt01801044", "pt01801045",
    "pt01801050", "pt01801051", "pt01801053", "pt01801054", "pt01801055", "pt01801056",
    "pt01801072", "pt01801074", "pt01801076", "pt01801077", "pt01801078",
    "pt01801080", "pt01801081", "pt01801082", "pt01801083", "pt01801084",
    "pt01801085", "pt01801086",
    "pt01801092", "pt01801093", "pt01801095", "pt01801096",
    "pt01801101", "pt01801102", "pt01801103", "pt01801104",
    "pt01801110", "pt01801111", "pt01801112", "pt01801113", "pt01801114",
    "pt01801115", "pt01801116",
    "pt01801120", "pt01801124", "pt01801125", "pt01801126", "pt01801127",
    "pt01801128", "pt01801129",
    "pt01801130", "pt01801131", "pt01801132", "pt01801133",
    "pt01801140", "pt01801141", "pt01801142", "pt01801143", "pt01801145",
    "pt01801150", "pt01801152", "pt01801153", "pt01801154", "pt01801155", "pt01801156",
    "pt01801160", "pt01801161", "pt01801163",
    "pt01801170", "pt01801178", "pt01801179",
    "pt01801180", "pt01801181", "pt01801183",
    "pt01801191", "pt01801193", "pt01801194",
    "pt01801200", "pt01801202", "pt01801203", "pt01801204", "pt01801206",
    "pt01801210", "pt01801218", "pt01801219",
    "pt01801223",
    "pt01801230", "pt01801231",
]

# 持仓 → 相关板块（手工映射到最接近的申万二级行业）
STOCK_SECTORS: dict[str, list[str]] = {
    "601698": ["pt01801102", "pt01801223"],  # 中国卫通: 通信设备 + 通信服务
    "000063": ["pt01801102", "pt01801083"],   # 中兴通讯: 通信设备 + 元件
    "002439": ["pt01801103", "pt01801104"],   # 启明星辰: IT服务 + 软件开发
}


# ═══════════════════════════════════════════════════════════════

def _fetch_tencent_batch(symbols: list[str]) -> str:
    url = "http://qt.gtimg.cn/q=" + ",".join(symbols)
    from .platform_compat import http_get_text
    raw = http_get_text(url, timeout=20, encoding="gbk")
    if not raw:
        logger.warning("腾讯板块数据获取失败")
    return raw


def _parse_dec(val: str) -> Decimal:
    val = (val or "").strip()
    if not val:
        return Decimal("0")
    try:
        return Decimal(val)
    except Exception:
        return Decimal("0")


def get_sectors() -> list[dict]:
    """获取所有板块行情（涨跌幅+量比，97个板块全部可用）。

    Returns:
        [{"code", "name", "chg_pct", "volume_ratio", "up_count", "down_count"}, ...]
        up_count/down_count 仅在数据有效时 > 0
    """
    results = []
    for i in range(0, len(SECTOR_CODES), 50):
        batch = SECTOR_CODES[i:i + 50]
        raw = _fetch_tencent_batch(batch)
        for line in raw.splitlines():
            line = line.strip()
            if "=" not in line or "~" not in line:
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
                name = parts[1]
                code = parts[2]
                if not name or name == "0":
                    continue
                chg_pct = _parse_dec(parts[32])
                vol_ratio = _parse_dec(parts[49])

                # 涨跌家数（best-effort，部分板块字段为资金流而非家数）
                up = down = 0
                try:
                    raw_up = int(float(parts[54] or 0))
                    raw_down = int(float(parts[52] or 0))
                    raw_flat = int(float(parts[53] or 0))
                    if raw_up >= 0 and raw_down >= 0 and raw_flat >= 0:
                        up, down = raw_up, raw_down
                except (ValueError, IndexError):
                    pass

                results.append({
                    "code": code,
                    "name": name,
                    "chg_pct": chg_pct,
                    "volume_ratio": vol_ratio,
                    "up_count": up,
                    "down_count": down,
                })
            except Exception:
                continue
    return results


def get_market_snapshot() -> dict:
    """全市场温度快照。

    Returns:
        {
            sector_count: int,
            up_sectors: int,      # 上涨板块数
            down_sectors: int,    # 下跌板块数
            avg_chg: Decimal,     # 平均涨跌幅
            top5: [{name, chg_pct}],
            bottom5: [{name, chg_pct}],
            breadth_up: int,      # 涨跌家数合计（仅有效板块，约16个）
            breadth_down: int,
            temperature: str,     # 🔥/🟢/🟡/🔴
        }
    """
    sectors = get_sectors()
    if not sectors:
        return {"sector_count": 0, "temperature": "无数据"}

    up_sectors = sum(1 for s in sectors if s["chg_pct"] > 0)
    down_sectors = sum(1 for s in sectors if s["chg_pct"] < 0)
    avg_chg = sum(s["chg_pct"] for s in sectors) / len(sectors)

    ranked = sorted(sectors, key=lambda x: x["chg_pct"], reverse=True)

    # 温度
    up_ratio = up_sectors / len(sectors) * 100
    if up_ratio > 70:
        temp = "🔥 普涨"
    elif up_ratio > 55:
        temp = "🟢 偏强"
    elif up_ratio > 40:
        temp = "🟡 分化"
    else:
        temp = "🔴 普跌"

    # 涨跌家数合计（仅有效板块）
    breadth_up = sum(s["up_count"] for s in sectors if s["up_count"] > 0)
    breadth_down = sum(s["down_count"] for s in sectors if s["down_count"] > 0)

    return {
        "sector_count": len(sectors),
        "up_sectors": up_sectors,
        "down_sectors": down_sectors,
        "avg_chg": avg_chg,
        "top5": [{"name": s["name"], "chg_pct": s["chg_pct"]} for s in ranked[:5]],
        "bottom5": [{"name": s["name"], "chg_pct": s["chg_pct"]} for s in ranked[-5:]],
        "breadth_up": breadth_up,
        "breadth_down": breadth_down,
        "temperature": temp,
    }


def get_holding_sector_ctx(stock_codes: list[str], sectors: Optional[list[dict]] = None) -> dict:
    """持仓股所在板块的行情上下文。"""
    all_sc = set()
    stock_map = {}
    for code in stock_codes:
        sc = STOCK_SECTORS.get(code, [])
        stock_map[code] = sc
        all_sc.update(sc)

    if not all_sc:
        return {}

    if sectors is None:
        sectors = get_sectors()
    all_sectors = {f"pt{s['code']}": s for s in sectors if s.get('code')}

    result = {}
    for code in stock_codes:
        sc = stock_map[code]
        infos = [all_sectors[c] for c in sc if c in all_sectors]
        if not infos:
            continue
        avg = sum(s["chg_pct"] for s in infos) / len(infos)
        if avg > 2:
            sent = "🟢强势"
        elif avg > 0:
            sent = "🟡偏强"
        elif avg > -2:
            sent = "🟠偏弱"
        else:
            sent = "🔴弱势"
        result[code] = {"sectors": infos, "avg_chg": avg, "sentiment": sent}

    return result


# ═══════════════════════════════════════════════════════════════
# Markdown
# ═══════════════════════════════════════════════════════════════

def format_breadth_md(stock_codes: Optional[list[str]] = None) -> str:
    """生成市场温度 + 板块强弱 + 持仓板块 的 markdown。"""
    lines = []

    # 1. 尝试 CDP 获取精确涨跌家数
    breadth_up = breadth_down = 0
    cdp_breadth = None
    try:
        from .chrome_scraper import get_market_breadth_cdp
        cdp_breadth = get_market_breadth_cdp()
    except Exception as exc:
        logger.warning("Chrome CDP market breadth fetch failed: %s", exc)

    # 2. 板块数据
    sectors = get_sectors()
    if not sectors and not cdp_breadth:
        return "市场数据暂不可用"

    lines = []

    # 市场温度（优先用 CDP 精确涨跌家数，fallback 到板块聚合）
    if cdp_breadth and cdp_breadth.get("total_up", 0) > 0:
        total_up = cdp_breadth["total_up"]
        total_down = cdp_breadth["total_down"]
        total_flat = cdp_breadth["sh_flat"] + cdp_breadth["sz_flat"]
        total_all = total_up + total_down + total_flat
        up_ratio_val = float(cdp_breadth["up_ratio"])
    else:
        up_sectors = sum(1 for s in sectors if s["chg_pct"] > 0)
        down_sectors = sum(1 for s in sectors if s["chg_pct"] < 0)
        total_up = up_sectors
        total_down = down_sectors
        up_ratio_val = up_sectors / len(sectors) * 100 if sectors else 0
        total_all = len(sectors)

    if up_ratio_val > 70:
        temp = "🔥 普涨"
    elif up_ratio_val > 55:
        temp = "🟢 偏强"
    elif up_ratio_val > 40:
        temp = "🟡 分化"
    else:
        temp = "🔴 普跌"

    # 板块聚合的涨跌家数（best-effort 补充）
    if not cdp_breadth:
        breadth_up = sum(s["up_count"] for s in sectors if s["up_count"] > 0)
        breadth_down = sum(s["down_count"] for s in sectors if s["down_count"] > 0)
    else:
        breadth_up = cdp_breadth["total_up"]
        breadth_down = cdp_breadth["total_down"]

    # 板块排名
    up_sectors = sum(1 for s in sectors if s["chg_pct"] > 0) if sectors else 0
    down_sectors = sum(1 for s in sectors if s["chg_pct"] < 0) if sectors else 0
    avg_chg = sum(s["chg_pct"] for s in sectors) / len(sectors) if sectors else Decimal("0")
    ranked = sorted(sectors, key=lambda x: x["chg_pct"], reverse=True) if sectors else []

    top5 = " > ".join(f"{s['name']} {s['chg_pct']:+.1f}%" for s in ranked[:5]) if ranked else "无数据"
    bot5 = " > ".join(f"{s['name']} {s['chg_pct']:+.1f}%" for s in ranked[-5:]) if ranked else "无数据"

    breadth_str = f" 涨跌比↑{breadth_up}/↓{breadth_down}" if breadth_up > 0 else ""

    lines.append(
        f"**市场温度** {temp} "
        f"（{up_sectors}涨/{down_sectors}跌/{len(sectors)}板块 "
        f"均值{avg_chg:+.1f}%）{breadth_str}"
    )
    lines.append(f"**最强**：{top5}")
    lines.append(f"**最弱**：{bot5}")

    # 持仓板块
    if stock_codes:
        ctx = get_holding_sector_ctx(stock_codes, sectors)
        if ctx:
            lines.append("")
            lines.append("**持仓板块**：")
            for code in stock_codes:
                info = ctx.get(code, {})
                si = info.get("sectors", [])
                if si:
                    parts = [f"{s['name']} {s['chg_pct']:+.1f}% 量比{s['volume_ratio']:.2f}" for s in si]
                    lines.append(f"  {code} [{info.get('sentiment', '?')}]：{' | '.join(parts)}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import json as _json

    snap = get_market_snapshot()
    print("=== 全市场温度 ===")
    print(_json.dumps(snap, ensure_ascii=False, default=str, indent=2))
    print()

    print("=== Top 10 ===")
    sectors = get_sectors()
    ranked = sorted(sectors, key=lambda x: x["chg_pct"], reverse=True)
    for s in ranked[:10]:
        up_str = f" ↑{s['up_count']}↓{s['down_count']}" if s['up_count'] > 0 else ""
        print(f"  {s['name']}: {s['chg_pct']:+.1f}% 量比{s['volume_ratio']:.2f}{up_str}")

    print("\n=== Bottom 10 ===")
    for s in ranked[-10:]:
        up_str = f" ↑{s['up_count']}↓{s['down_count']}" if s['up_count'] > 0 else ""
        print(f"  {s['name']}: {s['chg_pct']:+.1f}% 量比{s['volume_ratio']:.2f}{up_str}")

    print()
    print(format_breadth_md(["601698", "000063", "002439"]))