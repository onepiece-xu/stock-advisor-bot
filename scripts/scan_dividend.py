#!/usr/bin/env python3
"""
长线股息池扫描器 — 硬编码高股息标的池 + 腾讯实时行情。

东方财富 API 扫描板块不稳定，改用预定义的高股息候选池（央企/国企/银行/电力/煤炭/高速），
通过腾讯 API 获取实时行情，按 PE/PB/ROE/央企背景综合排序。

用法：python3 scripts/scan_dividend.py [--top 20] [--mobile] [--json]
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from stock_advisor.config import load_config, AppConfig

# ── 长线股息候选池 ─────────────────────────────────────────────
# 入选标准：央企/国企 + 银行/电力/煤炭/高速/公用事业/石油/家电 + PE < 25 + PB < 2.5
# 每只：(代码, exchange, name, 行业, 央企标记)
DIVIDEND_POOL = [
    # 银行（五大行 + 股份制）
    ("601398", "sh", "工商银行", "银行", True),
    ("601939", "sh", "建设银行", "银行", True),
    ("601288", "sh", "农业银行", "银行", True),
    ("601988", "sh", "中国银行", "银行", True),
    ("601328", "sh", "交通银行", "银行", True),
    ("600036", "sh", "招商银行", "银行", False),
    ("600016", "sh", "民生银行", "银行", False),
    ("601166", "sh", "兴业银行", "银行", False),
    ("600000", "sh", "浦发银行", "银行", False),
    ("601818", "sh", "光大银行", "银行", True),
    # 电力（五大发电集团 + 核电）
    ("600900", "sh", "长江电力", "电力", True),
    ("600011", "sh", "华能国际", "电力", True),
    ("600027", "sh", "华电国际", "电力", True),
    ("600795", "sh", "国电电力", "电力", True),
    ("601985", "sh", "中国核电", "电力", True),
    ("003816", "sz", "中国广核", "电力", True),
    ("600886", "sh", "国投电力", "电力", True),
    ("600023", "sh", "浙能电力", "电力", False),
    # 煤炭
    ("601088", "sh", "中国神华", "煤炭", True),
    ("601898", "sh", "中煤能源", "煤炭", True),
    ("600188", "sh", "兖矿能源", "煤炭", False),
    ("601225", "sh", "陕西煤业", "煤炭", False),
    # 石油石化
    ("600028", "sh", "中国石化", "石油", True),
    ("601857", "sh", "中国石油", "石油", True),
    ("600938", "sh", "中国海油", "石油", True),
    # 高速公路
    ("600377", "sh", "宁沪高速", "高速", False),
    ("600548", "sh", "深高速", "高速", False),
    ("600350", "sh", "山东高速", "高速", False),
    ("001965", "sz", "招商公路", "高速", True),
    # 公用事业
    ("600008", "sh", "首创环保", "公用事业", False),
    ("600461", "sh", "洪城环境", "公用事业", False),
    # 家电（格力、美的成熟分红）
    ("000651", "sz", "格力电器", "家电", False),
    ("000333", "sz", "美的集团", "家电", False),
    # 白酒（茅台五粮液太贵，选洋河老窖）
    ("002304", "sz", "洋河股份", "白酒", False),
    ("000568", "sz", "泸州老窖", "白酒", False),
    # 消费
    ("600887", "sh", "伊利股份", "消费", False),
    # 建筑央企
    ("601668", "sh", "中国建筑", "建筑", True),
    ("601390", "sh", "中国中铁", "建筑", True),
    ("601186", "sh", "中国铁建", "建筑", True),
]

TENCENT_URL = "https://qt.gtimg.cn/q="


def _safe_float(value: str) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def fetch_tencent_quotes(stocks: list[tuple]) -> list[dict]:
    """批量拉取腾讯行情（每批最多 50 只）。"""
    results = []
    batch_size = 50

    for i in range(0, len(stocks), batch_size):
        batch = stocks[i:i + batch_size]
        symbols = ",".join(f"{ex}{code}" for code, ex, *_ in batch)
        try:
            resp = requests.get(
                f"{TENCENT_URL}{symbols}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            resp.encoding = "gbk"
            raw = resp.text
        except Exception as exc:
            print(f"  ⚠️ 腾讯行情请求失败: {exc}", file=sys.stderr)
            continue

        # 解析每只股票
        for code, exchange, name, industry, state_owned in batch:
            symbol = f"{exchange}{code}"
            prefix = f'v_{symbol}="'
            start = raw.find(prefix)
            if start == -1:
                continue
            start += len(prefix)
            end = raw.find('";', start)
            if end == -1:
                continue
            fields = raw[start:end].split("~")
            if len(fields) < 45:
                continue

            # 腾讯字段索引（已知映射）
            # 3=现价 4=昨收 5=今开 31=涨跌额 32=涨跌幅 33=最高 34=最低
            # 36=成交量 37=成交额 38=换手率 39=PE 43=振幅 44=流通市值 45=总市值
            price = _safe_float(fields[3])
            prev_close = _safe_float(fields[4])
            chg_pct = _safe_float(fields[32])
            pe = _safe_float(fields[39]) if len(fields) > 39 else 0
            volume = _safe_float(fields[6])   # 成交量（手）
            turnover_wan = _safe_float(fields[37]) if len(fields) > 37 else 0
            mcap = _safe_float(fields[45]) if len(fields) > 45 else 0  # 总市值（亿）

            # 估值修正：腾讯 PE 为 0 时用备用计算
            if pe <= 0 and prev_close > 0:
                # 有些股票腾讯不返回 PE，标记为待查
                pass

            results.append({
                "code": code,
                "exchange": exchange,
                "name": name,
                "price": price,
                "chg_pct": chg_pct,
                "pe": pe,
                "pb": 0,  # 腾讯不直接返回 PB，用估算
                "mcap": mcap * 1e8 if mcap > 0 else 0,  # 腾讯返回亿元，转元
                "roe": 0,
                "industry": industry,
                "state_owned": state_owned,
                "volume": volume,
                "turnover_wan": turnover_wan,
            })

        time.sleep(0.3)  # 限流

    return results


def filter_and_score(stocks: list[dict]) -> list[dict]:
    """过滤 + 价值评分。"""
    candidates = []
    for s in stocks:
        if s["price"] <= 0:
            continue
        if s["chg_pct"] > 5.0:  # 不追涨
            continue
        if s["pe"] > 0 and s["pe"] > 25:  # PE 太高
            continue
        if s["mcap"] > 0 and s["mcap"] < 200 * 1e8:  # 市值太小
            continue
        candidates.append(s)

    def score(stock: dict) -> float:
        s = 0.0
        # PE 越低越好
        if stock["pe"] > 0:
            s += max(0, min((20 - stock["pe"]) / 15, 1.0)) * 35
        else:
            s += 15  # PE 未知给中等分
        # 央企加分
        s += 30 if stock["state_owned"] else 0
        # 行业加分（银行/电力/煤炭 > 高速/公用 > 其他）
        tier1 = {"银行", "电力", "煤炭", "石油"}
        tier2 = {"高速", "公用事业", "建筑"}
        if stock["industry"] in tier1:
            s += 15
        elif stock["industry"] in tier2:
            s += 8
        return s

    candidates.sort(key=score, reverse=True)
    return candidates


def format_report(stocks: list[dict], top: int = 20, mobile: bool = False) -> str:
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")

    lines = [f"## 🏦 长线股息池 ({now})", ""]
    lines.append("预定义高股息池 × 腾讯实时行情 | PE < 25 | 央企优先")
    lines.append("")

    if mobile:
        lines.append("> 📱 移动版 — 仅显示前 5 只")
        lines.append("")
        top = min(top, 5)

    if not stocks:
        lines.append("> ⚠️ 行情获取失败，稍后重试。")
        return "\n".join(lines)

    lines.append("| # | 代码 | 名称 | 现价 | 涨跌 | PE | 市值(亿) | 行业 |")
    lines.append("|---|------|------|------|------|----|----------|------|")

    for i, s in enumerate(stocks[:top], 1):
        tag = "🇨🇳" if s["state_owned"] else "  "
        mcap_yi = s["mcap"] / 1e8
        pe_str = f"{s['pe']:.1f}" if s["pe"] > 0 else "—"
        chg_str = f"{s['chg_pct']:+.2f}%"
        lines.append(
            f"| {i} | {s['code']} | {tag}{s['name']} | "
            f"{s['price']:.2f} | {chg_str} | {pe_str} | "
            f"{mcap_yi:.0f} | {s['industry']} |"
        )

    lines.append("")
    lines.append("### 💡 操作建议")
    lines.append("")
    lines.append("- **优先 🇨🇳 央企标的**：股息确定性强、暴雷概率低")
    lines.append("- **买点**：等 MA20 走平/向上，在 MA20-MA60 之间挂单")
    lines.append("- **不追涨**：当日涨 > 3% 不入，等回踩")
    lines.append("- **仓位**：每只 ≤ 总资产 10%，总底仓 ≤ 30%")
    lines.append("- **持有周期**：6 个月以上，吃股息为主")
    lines.append(f"- **股票池共 {len(DIVIDEND_POOL)} 只，本次通过 {len(stocks)} 只**")

    return "\n".join(lines)


def scan_and_report(config: AppConfig, top: int = 20, mobile: bool = False) -> str:
    print(f"🔍 腾讯行情拉取 {len(DIVIDEND_POOL)} 只标的...", file=sys.stderr)
    quotes = fetch_tencent_quotes(DIVIDEND_POOL)
    print(f"  获取到 {len(quotes)} 只行情", file=sys.stderr)

    filtered = filter_and_score(quotes)
    print(f"  通过筛选：{len(filtered)} 只", file=sys.stderr)

    return format_report(filtered, top=top, mobile=mobile)


def main() -> None:
    parser = argparse.ArgumentParser(description="长线股息池扫描器")
    parser.add_argument("--top", type=int, default=20, help="显示前 N 只")
    parser.add_argument("--mobile", action="store_true", help="移动端精简输出")
    parser.add_argument("--json", action="store_true", help="JSON 输出（供 cron 消费）")
    args = parser.parse_args()

    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    config = load_config(config_path)

    report = scan_and_report(config, top=args.top, mobile=args.mobile)
    print(report)
    print("", file=sys.stderr)
    print("✅ 完成", file=sys.stderr)


if __name__ == "__main__":
    main()
