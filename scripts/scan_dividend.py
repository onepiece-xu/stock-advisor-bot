#!/usr/bin/env python3
"""
长线股息池扫描器 v2 — 硬编码高股息标的池 + 腾讯实时行情 + MA20 买点区。

v2 改进：
  - 通过腾讯日K API 计算 MA20，给出挂单区间
  - ⭐ 标记可动手标的（现价 ≤ MA20×1.02 且今日涨幅 ≤ 2%）
  - ⏳ 标记需等待标的（涨超 2% 或远离 MA20）

用法：python3 scripts/scan_dividend.py [--top 20] [--mobile]
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
DIVIDEND_POOL = [
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
    ("600900", "sh", "长江电力", "电力", True),
    ("600011", "sh", "华能国际", "电力", True),
    ("600027", "sh", "华电国际", "电力", True),
    ("600795", "sh", "国电电力", "电力", True),
    ("601985", "sh", "中国核电", "电力", True),
    ("003816", "sz", "中国广核", "电力", True),
    ("600886", "sh", "国投电力", "电力", True),
    ("600023", "sh", "浙能电力", "电力", False),
    ("601088", "sh", "中国神华", "煤炭", True),
    ("601898", "sh", "中煤能源", "煤炭", True),
    ("600188", "sh", "兖矿能源", "煤炭", False),
    ("601225", "sh", "陕西煤业", "煤炭", False),
    ("600028", "sh", "中国石化", "石油", True),
    ("601857", "sh", "中国石油", "石油", True),
    ("600938", "sh", "中国海油", "石油", True),
    ("600377", "sh", "宁沪高速", "高速", False),
    ("600548", "sh", "深高速", "高速", False),
    ("600350", "sh", "山东高速", "高速", False),
    ("001965", "sz", "招商公路", "高速", True),
    ("600008", "sh", "首创环保", "公用事业", False),
    ("600461", "sh", "洪城环境", "公用事业", False),
    ("000651", "sz", "格力电器", "家电", False),
    ("000333", "sz", "美的集团", "家电", False),
    ("002304", "sz", "洋河股份", "白酒", False),
    ("000568", "sz", "泸州老窖", "白酒", False),
    ("600887", "sh", "伊利股份", "消费", False),
    ("601668", "sh", "中国建筑", "建筑", True),
    ("601390", "sh", "中国中铁", "建筑", True),
    ("601186", "sh", "中国铁建", "建筑", True),
]

TENCENT_QUOTE = "https://qt.gtimg.cn/q="
TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def _safe_float(value: str) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


# ── 腾讯实时行情 ──────────────────────────────────────────────

def fetch_tencent_quotes(stocks: list[tuple]) -> list[dict]:
    """批量拉取腾讯实时行情（每批 50 只）。"""
    results = []
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"

    for i in range(0, len(stocks), 50):
        batch = stocks[i:i + 50]
        symbols = ",".join(f"{ex}{code}" for code, ex, *_ in batch)
        try:
            resp = session.get(f"{TENCENT_QUOTE}{symbols}", timeout=10)
            resp.encoding = "gbk"
            raw = resp.text
        except Exception as exc:
            print(f"  ⚠️ 行情请求失败: {exc}", file=sys.stderr)
            continue

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

            price = _safe_float(fields[3])
            chg_pct = _safe_float(fields[32])
            pe = _safe_float(fields[39]) if len(fields) > 39 else 0
            mcap = _safe_float(fields[45]) if len(fields) > 45 else 0

            results.append({
                "code": code,
                "exchange": exchange,
                "symbol": symbol,
                "name": name,
                "price": price,
                "chg_pct": chg_pct,
                "pe": pe,
                "mcap": mcap * 1e8 if mcap > 0 else 0,
                "industry": industry,
                "state_owned": state_owned,
                "ma20": 0.0,
                "buy_zone": "",
                "actionable": False,
                "wait_reason": "",
            })

        time.sleep(0.3)

    session.close()
    return results


# ── MA20 获取（腾讯日K API）───────────────────────────────────

def fetch_ma20_batch(stocks: list[dict]) -> None:
    """为 stock dict 填充 MA20（逐个请求以兼容腾讯 K 线 API）。"""
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"

    for i, s in enumerate(stocks):
        symbol = s["symbol"]
        try:
            url = f"{TENCENT_KLINE}?param={symbol},day,,,30,qfq"
            resp = session.get(url, timeout=8)
            data = resp.json()
            klines = (
                data.get("data", {}).get(symbol, {}).get("qfqday", [])
                or data.get("data", {}).get(symbol, {}).get("day", [])
            )
            if klines and len(klines) >= 20:
                closes = [float(k[2]) for k in klines[-20:]]
                s["ma20"] = sum(closes) / len(closes)
        except Exception:
            pass

        if i % 10 == 9 or i == len(stocks) - 1:
            print(f"  MA20 进度: {i + 1}/{len(stocks)}", file=sys.stderr)
        time.sleep(0.15)

    session.close()


def compute_buy_zone(s: dict) -> None:
    """根据 MA20 和当日涨跌计算买入建议。"""
    ma20 = s["ma20"]
    price = s["price"]
    chg = s["chg_pct"]

    if ma20 <= 0:
        s["buy_zone"] = "MA20 未知"
        s["wait_reason"] = "数据缺失"
        return

    # 买点：MA20 下方 2% 到 MA20 上方 2%
    zone_low = round(ma20 * 0.98, 2)
    zone_high = round(ma20 * 1.02, 2)

    if chg > 2.0:
        s["buy_zone"] = f"{zone_low}-{zone_high}"
        s["wait_reason"] = "今日涨超 2%，等回踩"
    elif price > ma20 * 1.05:
        s["buy_zone"] = f"{zone_low}-{zone_high}"
        s["wait_reason"] = f"高于 MA20({ma20:.2f}) 5%+，等回调"
    elif price < ma20 * 0.95:
        s["buy_zone"] = f"{zone_low}-{zone_high}"
        s["wait_reason"] = "低于 MA20 5%+，确认趋势再入"
    else:
        s["buy_zone"] = f"{zone_low}-{zone_high}"
        s["actionable"] = True


# ── 过滤 + 评分 ────────────────────────────────────────────────

def filter_and_score(stocks: list[dict]) -> list[dict]:
    candidates = []
    for s in stocks:
        if s["price"] <= 0:
            continue
        if s["pe"] > 0 and s["pe"] > 25:
            continue
        if s["mcap"] > 0 and s["mcap"] < 200 * 1e8:
            continue
        candidates.append(s)

    def score(stock: dict) -> float:
        s = 0.0
        if stock["pe"] > 0:
            s += max(0, min((20 - stock["pe"]) / 15, 1.0)) * 30
        else:
            s += 10
        s += 25 if stock["state_owned"] else 0
        # 可动手加分
        s += 15 if stock["actionable"] else 0
        # 行业加分
        tier1 = {"银行", "电力", "煤炭", "石油"}
        if stock["industry"] in tier1:
            s += 10
        elif stock["industry"] in {"高速", "公用事业", "建筑"}:
            s += 5
        return s

    candidates.sort(key=score, reverse=True)
    return candidates


# ── 报告输出 ──────────────────────────────────────────────────

def format_report(stocks: list[dict], top: int = 20, mobile: bool = False) -> str:
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    actionable = [s for s in stocks if s["actionable"]]

    lines = [f"## 🏦 长线股息池 ({now})", ""]
    lines.append(f"⭐ 可动手 {len(actionable)} 只 | PE < 25 | 央企优先 | MA20 附近挂单")
    lines.append("")

    if mobile:
        top = min(top, 5)

    if not stocks:
        lines.append("> ⚠️ 行情获取失败")
        return "\n".join(lines)

    # 表头
    lines.append("| # | 代码 | 名称 | 现价 | 涨跌 | PE | MA20 | 挂单区间 | 行业 |")
    lines.append("|---|------|------|------|------|----|------|----------|------|")

    for i, s in enumerate(stocks[:top], 1):
        tag = "🇨🇳" if s["state_owned"] else "  "
        action_mark = "⭐" if s["actionable"] else "⏳"
        chg_str = f"{s['chg_pct']:+.2f}%"
        pe_str = f"{s['pe']:.1f}" if s["pe"] > 0 else "—"
        ma_str = f"{s['ma20']:.2f}" if s["ma20"] > 0 else "—"

        lines.append(
            f"| {i} | {s['code']} | {tag}{action_mark}{s['name']} | "
            f"{s['price']:.2f} | {chg_str} | {pe_str} | {ma_str} | "
            f"{s['buy_zone']} | {s['industry']} |"
        )

    # 可动手汇总
    if actionable:
        lines.append("")
        lines.append("### ⭐ 今天能动手的")
        lines.append("")
        for s in actionable[:5]:
            lines.append(
                f"- **{s['name']}({s['code']})** — 现价 {s['price']:.2f}，"
                f"PE {s['pe']:.1f}，挂单 {s['buy_zone']}，一手约 {s['price']*100:.0f} 元"
            )

    # 需等待
    waiting = [s for s in stocks[:top] if not s["actionable"] and s["wait_reason"]]
    if waiting:
        lines.append("")
        lines.append("### ⏳ 需要等的")
        lines.append("")
        for s in waiting[:5]:
            lines.append(f"- {s['name']}({s['code']}) — {s['wait_reason']}")

    lines.append("")
    lines.append("### 💡 纪律")
    lines.append("- 每只 ≤ 总资产 10%，总底仓 ≤ 30%")
    lines.append("- ⭐ 标的在 MA20 附近直接挂单，不追高")
    lines.append("- ⏳ 标的加入自选等回调到 MA20")
    lines.append(f"- 池子共 {len(DIVIDEND_POOL)} 只，通过 {len(stocks)} 只")

    return "\n".join(lines)


# ── 主流程 ────────────────────────────────────────────────────

def scan_and_report(config: AppConfig, top: int = 20, mobile: bool = False) -> str:
    print(f"📡 拉取 {len(DIVIDEND_POOL)} 只实时行情...", file=sys.stderr)
    stocks = fetch_tencent_quotes(DIVIDEND_POOL)
    print(f"  获取 {len(stocks)} 只", file=sys.stderr)

    print(f"📊 拉取 MA20（日K线）...", file=sys.stderr)
    fetch_ma20_batch(stocks)

    for s in stocks:
        compute_buy_zone(s)

    filtered = filter_and_score(stocks)
    print(f"  通过筛选: {len(filtered)} 只, 可动手 {sum(1 for s in filtered if s['actionable'])} 只", file=sys.stderr)

    return format_report(filtered, top=top, mobile=mobile)


def main() -> None:
    parser = argparse.ArgumentParser(description="长线股息池扫描器 v2")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--mobile", action="store_true")
    args = parser.parse_args()

    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    config = load_config(config_path)

    report = scan_and_report(config, top=args.top, mobile=args.mobile)
    print(report)
    print("", file=sys.stderr)
    print("✅ 完成", file=sys.stderr)


if __name__ == "__main__":
    main()
