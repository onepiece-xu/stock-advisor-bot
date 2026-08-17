"""
技术面快照模块 — 持仓票全量技术指标一览

输出字段：
  - 现价 / 涨跌幅 / 日内振幅
  - MA5 / MA15 / MA60 位置及乖离率
  - RSI(14)
  - 量比（当日量 vs 5日均量）
  - 板块排名 / 板块涨跌
  - 相对大盘强弱
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

logger = logging.getLogger(__name__)


# ── helpers ──────────────────────────────────────────────────

def _safe_decimal(val: Any, default: Decimal = Decimal("0")) -> Decimal:
    if val is None:
        return default
    try:
        return Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0")


def _tencent_symbol(code: str) -> str:
    """Convert 6-digit code to Tencent symbol: sh601698 / sz000063."""
    if code.startswith(("6", "9")):
        return f"sh{code}"
    return f"sz{code}"


def _simple_ma(prices: list[Decimal], n: int) -> Decimal:
    if len(prices) < n:
        return Decimal("0")
    return (sum(prices[-n:]) / n).quantize(Decimal("0.01"))


def _simple_rsi(prices: list[Decimal], n: int = 14) -> Decimal:
    if len(prices) < n + 1:
        return Decimal("50")
    gains = Decimal("0")
    losses = Decimal("0")
    for i in range(-n, 0):
        diff = prices[i] - prices[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses += abs(diff)
    if losses == 0:
        return Decimal("100")
    rs = gains / losses
    return (Decimal("100") - (Decimal("100") / (Decimal("1") + rs))).quantize(Decimal("0.01"))


def _daily_amplitude(highs: list[Decimal], lows: list[Decimal], prev_close: Decimal) -> Decimal:
    """日内振幅 = (最高-最低)/昨收 * 100"""
    if not highs or not lows or prev_close <= 0:
        return Decimal("0")
    return ((highs[-1] - lows[-1]) / prev_close * Decimal("100")).quantize(Decimal("0.01"))


# ── data fetching ─────────────────────────────────────────────

def _fetch_realtime_quotes(codes: list[str]) -> dict[str, dict]:
    """Fetch real-time quotes from Tencent batch API."""
    if not codes:
        return {}
    symbols = ",".join(_tencent_symbol(c) for c in codes)
    url = f"https://qt.gtimg.cn/q={symbols}"
    try:
        from .platform_compat import http_get_bytes
        raw = http_get_bytes(url, timeout=10)
        if not raw:
            return {}
        # Tencent returns GBK, handle encoding
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")

        quotes: dict[str, dict] = {}
        for line in text.strip().split("\n"):
            if "=" not in line or "~" not in line:
                continue
            try:
                # Extract symbol code from "v_sh601698=\"...\""
                prefix = line.split("=")[0]
                code_part = prefix.replace("v_", "").replace("sh", "").replace("sz", "")
                fields = line.split('"')[1].split("~") if '"' in line else []
                if len(fields) < 45:
                    continue
                quotes[code_part] = {
                    "name": fields[1],
                    "current_price": _safe_decimal(fields[3]),
                    "previous_close": _safe_decimal(fields[4]),
                    "open_price": _safe_decimal(fields[5]),
                    "volume": int(fields[6]) if fields[6].isdigit() else 0,
                    "high_price": _safe_decimal(fields[33]),
                    "low_price": _safe_decimal(fields[34]),
                    "change_pct": _safe_decimal(fields[32]),
                    "turnover_rate": _safe_decimal(fields[38]),
                }
            except Exception:
                continue
        return quotes
    except Exception:
        return {}


def _fetch_daily_klines(codes: list[str], ndays: int = 60) -> dict[str, dict]:
    """Fetch daily K-line data from Tencent fqkline API.
    
    Returns dict[code -> {closes, highs, lows, volumes}]
    """
    results: dict[str, dict] = {}
    for code in codes:
        symbol = _tencent_symbol(code)
        url = (
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?param={symbol},day,,,{ndays},qfq"
        )
        try:
            from .platform_compat import http_get_text
            text = http_get_text(url, timeout=10, encoding="utf-8")
            if not text:
                continue
            data = json.loads(text)
            rows = data.get("data", {}).get(symbol, {}).get("qfqday", [])
            if not rows:
                rows = data.get("data", {}).get(symbol, {}).get("day", [])
            if not rows:
                continue
            # Each row: [date, open, close, high, low, volume]
            closes = [_safe_decimal(r[2]) for r in rows if len(r) >= 3]
            highs = [_safe_decimal(r[3]) for r in rows if len(r) >= 4]
            lows = [_safe_decimal(r[4]) for r in rows if len(r) >= 5]
            volumes = [_safe_decimal(str(r[5])) if len(r) >= 6 and str(r[5]).replace(".", "").isdigit() else Decimal("0") for r in rows if len(r) >= 6]
            results[code] = {
                "closes": closes,
                "highs": highs,
                "lows": lows,
                "volumes": volumes,
            }
        except Exception:
            continue
    return results


def _fetch_sector_strength() -> dict[str, dict]:
    """Fetch sector/board strength data. Returns {sector_name: {change_pct, rank}}."""
    try:
        from .sector_strength import fetch_sector_boards
        boards = fetch_sector_boards()
        result: dict[str, dict] = {}
        for i, b in enumerate(boards[:30], start=1):
            name = b.get("board_name", "")
            if name:
                result[name] = {
                    "change_pct": _safe_decimal(b.get("change_pct", 0)),
                    "rank": i,
                }
        return result
    except Exception:
        return {}


# ── stock-to-sector mapping ──────────────────────────────────

# Map stock codes to their primary sector/board names
CODE_SECTOR_MAP: dict[str, str] = {
    "601698": "航天航空",       # 中国卫通 → 航天航空
    "000063": "通信设备",       # 中兴通讯 → 通信设备
    "002439": "信息安全",       # 启明星辰 → 信息安全
    # Common stocks from the universe
    "600118": "航天航空",
    "600879": "航天航空",
    "600391": "航天航空",
    "600893": "航天航空",
    "600760": "航天航空",
    "000768": "航天航空",
    "600038": "航天航空",
    "002023": "航天航空",
    "002013": "航天航空",
    "000547": "航天航空",
    "002049": "半导体",
    "603986": "半导体",
    "688981": "半导体",
    "002185": "半导体",
    "688256": "半导体",
    "688012": "半导体",
    "002156": "半导体",
    "603501": "半导体",
    "600584": "半导体",
    "688396": "半导体",
    "300493": "通信设备",
    "002281": "通信设备",
    "600498": "通信设备",
    "300308": "通信设备",
    "000938": "通信设备",
    "002396": "通信设备",
    "300502": "通信设备",
    "300394": "通信设备",
    "002402": "消费电子",
    "300782": "消费电子",
    "002475": "消费电子",
    "002241": "消费电子",
    "688036": "消费电子",
    "300433": "消费电子",
    "002456": "消费电子",
    "002920": "汽车零部件",
    "300496": "汽车零部件",
    "601689": "汽车零部件",
    "002050": "汽车零部件",
    "601799": "汽车零部件",
    "002594": "汽车整车",
    "300750": "电池",
    "002460": "电池",
    "002466": "电池",
    "300014": "电池",
    "002074": "电池",
    "688005": "电池",
    "300274": "光伏设备",
    "601012": "光伏设备",
    "688599": "光伏设备",
    "002459": "光伏设备",
    "688223": "光伏设备",
    "300763": "光伏设备",
    "300124": "工业自动化",
    "688777": "工业自动化",
    "300454": "信息安全",
    "002212": "信息安全",
    "300188": "信息安全",
    "688561": "信息安全",
    "002268": "信息安全",
    "300369": "信息安全",
    "300033": "金融科技",
    "300059": "金融科技",
    "600570": "金融科技",
    "002410": "软件服务",
    "300803": "软件服务",
    "300624": "软件服务",
    "688111": "软件服务",
    "002230": "人工智能",
    "688088": "人工智能",
    "300678": "人工智能",
    "002415": "安防设备",
    "002236": "安防设备",
    "603160": "芯片设计",
    "300661": "芯片设计",
    "600703": "光学光电子",
    "300735": "光学光电子",
    "688981b": "半导体",
}


def _sector_for_code(code: str) -> str:
    return CODE_SECTOR_MAP.get(code, "")


# ── dataclass ─────────────────────────────────────────────────

@dataclass
class TechSnapshot:
    code: str
    name: str
    current_price: Decimal
    change_pct: Decimal
    amplitude_pct: Decimal        # 日内振幅
    ma5: Decimal
    ma15: Decimal
    ma60: Decimal
    bias_ma5: Decimal             # 乖离MA5
    bias_ma15: Decimal
    bias_ma60: Decimal
    rsi14: Decimal
    volume_ratio: Decimal         # 当日量 / 5日均量
    turnover_rate: Decimal
    sector: str
    sector_rank: int              # 板块排名
    sector_change_pct: Decimal    # 板块涨跌


# ── main compute ──────────────────────────────────────────────

def compute_tech_snapshots(codes: list[str], names: dict[str, str] | None = None) -> list[TechSnapshot]:
    """Compute full technical snapshot for a list of stock codes.
    
    Args:
        codes: Stock codes (6-digit, like '601698')
        names: Optional code→name mapping
    
    Returns:
        List of TechSnapshot sorted by code
    """
    if not codes:
        return []

    names = names or {}
    quotes = _fetch_realtime_quotes(codes)
    klines = _fetch_daily_klines(codes, ndays=60)
    sectors = _fetch_sector_strength()

    results: list[TechSnapshot] = []
    for code in codes:
        q = quotes.get(code, {})
        k = klines.get(code, {})
        name = q.get("name", "") or names.get(code, code)

        current_price = q.get("current_price", Decimal("0"))
        prev_close = q.get("previous_close", Decimal("0"))
        change_pct = q.get("change_pct", Decimal("0"))
        turnover = q.get("turnover_rate", Decimal("0"))

        closes = k.get("closes", [])
        highs = k.get("highs", [])
        lows = k.get("lows", [])
        volumes = k.get("volumes", [])

        # MA
        ma5 = _simple_ma(closes, 5)
        ma15 = _simple_ma(closes, 15)
        ma60 = _simple_ma(closes, 60)

        # Bias
        bias_ma5 = ((current_price - ma5) / ma5 * 100).quantize(Decimal("0.01")) if ma5 > 0 else Decimal("0")
        bias_ma15 = ((current_price - ma15) / ma15 * 100).quantize(Decimal("0.01")) if ma15 > 0 else Decimal("0")
        bias_ma60 = ((current_price - ma60) / ma60 * 100).quantize(Decimal("0.01")) if ma60 > 0 else Decimal("0")

        # RSI
        rsi14 = _simple_rsi(closes, 14)

        # Amplitude
        amplitude = _daily_amplitude(highs, lows, prev_close)

        # Volume ratio: today's volume vs 5-day average volume
        avg5_vol = _simple_ma(volumes, 5) if len(volumes) >= 5 else Decimal("1")
        today_vol = volumes[-1] if volumes else Decimal("0")
        vol_ratio = (today_vol / avg5_vol).quantize(Decimal("0.01")) if avg5_vol > 0 else Decimal("0")

        # Sector
        sector_name = _sector_for_code(code)
        sector_info = sectors.get(sector_name, {})
        sector_rank = sector_info.get("rank", 0)
        sector_chg = sector_info.get("change_pct", Decimal("0"))

        results.append(TechSnapshot(
            code=code,
            name=name,
            current_price=current_price,
            change_pct=change_pct,
            amplitude_pct=amplitude,
            ma5=ma5,
            ma15=ma15,
            ma60=ma60,
            bias_ma5=bias_ma5,
            bias_ma15=bias_ma15,
            bias_ma60=bias_ma60,
            rsi14=rsi14,
            volume_ratio=vol_ratio,
            turnover_rate=turnover,
            sector=sector_name,
            sector_rank=sector_rank,
            sector_change_pct=sector_chg,
        ))

    results.sort(key=lambda x: x.code)
    return results


# ── formatting ────────────────────────────────────────────────

def _fmt_price(v: Decimal) -> str:
    return f"{v:.2f}"


def _fmt_pct(v: Decimal) -> str:
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f}%"


def _fmt_ratio(v: Decimal) -> str:
    return f"{v:.2f}x"


def render_tech_snapshot_table(snapshots: list[TechSnapshot]) -> str:
    """Render technical snapshot as a markdown table."""
    if not snapshots:
        return "无持仓数据"

    lines = [
        "## 📊 技术面全貌",
        "",
        "| 标的 | 现价 | 涨跌 | 振幅 | MA5 | MA15 | MA60 | RSI | 量比 | 板块 |",
        "|------|------|------|------|-----|------|------|-----|------|------|",
    ]

    for s in snapshots:
        ma5_str = f"{_fmt_price(s.ma5)} ({_fmt_pct(s.bias_ma5)})"
        ma15_str = f"{_fmt_price(s.ma15)} ({_fmt_pct(s.bias_ma15)})"
        ma60_str = f"{_fmt_price(s.ma60)} ({_fmt_pct(s.bias_ma60)})"
        sector_str = f"{s.sector}#{s.sector_rank} {_fmt_pct(s.sector_change_pct)}" if s.sector else "—"

        line = (
            f"| {s.name} | {_fmt_price(s.current_price)} | {_fmt_pct(s.change_pct)} "
            f"| {_fmt_pct(s.amplitude_pct)} | {ma5_str} | {ma15_str} | {ma60_str} "
            f"| {s.rsi14} | {_fmt_ratio(s.volume_ratio)} | {sector_str} |"
        )
        lines.append(line)

    # Detailed per-stock section
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("### 📋 逐票详解")
    lines.append("")

    for s in snapshots:
        ma_stack = ""
        prices = [s.current_price, s.ma5, s.ma15, s.ma60]
        valid = [p for p in prices if p > 0]
        if len(valid) >= 2:
            if all(valid[i] > valid[i+1] for i in range(len(valid)-1)):
                ma_stack = "🟢 多头排列"
            elif all(valid[i] < valid[i+1] for i in range(len(valid)-1)):
                ma_stack = "🔴 空头排列"
            else:
                ma_stack = "🟡 交织"

        vol_note = ""
        if s.volume_ratio >= 2:
            vol_note = "🔥 放量"
        elif s.volume_ratio >= 1.2:
            vol_note = "📈 温和放量"
        elif s.volume_ratio >= 0.8:
            vol_note = "➡️ 平量"
        elif s.volume_ratio > 0:
            vol_note = "📉 缩量"

        rsi_note = ""
        if s.rsi14 >= 70:
            rsi_note = "⚠️ 超买"
        elif s.rsi14 <= 30:
            rsi_note = "💡 超卖"
        elif s.rsi14 >= 50:
            rsi_note = "偏强"
        else:
            rsi_note = "偏弱"

        lines.append(f"**{s.name}** ({s.code})")
        lines.append(f"  现价 {_fmt_price(s.current_price)} | {_fmt_pct(s.change_pct)} | 振幅 {_fmt_pct(s.amplitude_pct)}")
        lines.append(f"  MA排列: {ma_stack} | RSI: {s.rsi14} ({rsi_note}) | 量能: {_fmt_ratio(s.volume_ratio)} {vol_note}")
        lines.append(f"  MA5: {_fmt_price(s.ma5)} | MA15: {_fmt_price(s.ma15)} | MA60: {_fmt_price(s.ma60)}")
        if s.sector:
            lines.append(f"  板块: {s.sector} #{s.sector_rank} | 板块涨跌 {_fmt_pct(s.sector_change_pct)}")
        lines.append(f"  换手率: {_fmt_pct(s.turnover_rate)}")
        lines.append("")

    return "\n".join(lines)


def render_compare_table(snapshots: list[TechSnapshot], holding_info: dict[str, dict] | None = None) -> str:
    """Render side-by-side comparison with holdings context."""
    if not snapshots:
        return "无持仓数据"

    holding_info = holding_info or {}
    lines = [
        "## 📊 持仓同框对比",
        "",
    ]

    # Sort by change_pct descending (strongest first)
    sorted_snap = sorted(snapshots, key=lambda x: x.change_pct, reverse=True)

    # Head-to-head metrics table
    lines.append("| 指标 | " + " | ".join(s.name for s in sorted_snap) + " |")
    lines.append("|------|" + "|".join("------" for _ in sorted_snap) + "|")

    # Price row
    lines.append("| **现价** | " + " | ".join(_fmt_price(s.current_price) for s in sorted_snap) + " |")
    lines.append("| **涨跌幅** | " + " | ".join(_fmt_pct(s.change_pct) for s in sorted_snap) + " |")
    lines.append("| **日内振幅** | " + " | ".join(_fmt_pct(s.amplitude_pct) for s in sorted_snap) + " |")
    lines.append("| **RSI(14)** | " + " | ".join(str(s.rsi14) for s in sorted_snap) + " |")
    lines.append("| **量比** | " + " | ".join(_fmt_ratio(s.volume_ratio) for s in sorted_snap) + " |")
    lines.append("| **MA5** | " + " | ".join(_fmt_price(s.ma5) for s in sorted_snap) + " |")
    lines.append("| **MA15** | " + " | ".join(_fmt_price(s.ma15) for s in sorted_snap) + " |")
    lines.append("| **MA60** | " + " | ".join(_fmt_price(s.ma60) for s in sorted_snap) + " |")
    lines.append("| **MA5乖离** | " + " | ".join(_fmt_pct(s.bias_ma5) for s in sorted_snap) + " |")
    lines.append("| **MA15乖离** | " + " | ".join(_fmt_pct(s.bias_ma15) for s in sorted_snap) + " |")
    lines.append("| **MA60乖离** | " + " | ".join(_fmt_pct(s.bias_ma60) for s in sorted_snap) + " |")

    if any(s.sector for s in sorted_snap):
        lines.append("| **板块** | " + " | ".join(
            f"{s.sector}#{s.sector_rank}" if s.sector else "—" for s in sorted_snap
        ) + " |")

    # Add holdings context if available
    if holding_info:
        lines.append("")
        lines.append("### 💼 持仓信息")
        lines.append("")
        lines.append("| 标的 | 持仓 | 成本 | 现价 | 浮盈 |")
        lines.append("|------|------|------|------|------|")
        for s in sorted_snap:
            hi = holding_info.get(s.code, {})
            qty = hi.get("quantity", 0)
            cost = hi.get("cost_price", Decimal("0"))
            if qty > 0 and cost > 0:
                pnl = (s.current_price - cost) / cost * 100
                pnl_str = _fmt_pct(pnl.quantize(Decimal("0.01")) if hasattr(pnl, 'quantize') else pnl)
                lines.append(
                    f"| {s.name} | {qty}股 | {_fmt_price(cost)} | {_fmt_price(s.current_price)} | {pnl_str} |"
                )
            else:
                lines.append(f"| {s.name} | — | — | {_fmt_price(s.current_price)} | — |")

    # Strength ranking
    lines.append("")
    lines.append("### 🏆 强弱排序")
    for i, s in enumerate(sorted_snap, 1):
        emoji = "🟢" if s.change_pct > 1 else ("🔴" if s.change_pct < -1 else "🟡")
        lines.append(f"{i}. {emoji} **{s.name}** — {_fmt_pct(s.change_pct)} | RSI {s.rsi14} | 量比 {_fmt_ratio(s.volume_ratio)}")

    return "\n".join(lines)


# ── reconciliation ───────────────────────────────────────────

@dataclass
class HoldingDiff:
    code: str
    name: str
    snapshot_qty: int
    screenshot_qty: int | None
    snapshot_cost: Decimal
    screenshot_cost: Decimal | None
    has_discrepancy: bool


def compare_holdings(
    snapshot_holdings: list[dict],
    screenshot_holdings: list[dict],
) -> list[HoldingDiff]:
    """Compare portfolio-snapshot.json holdings with screenshot-parsed holdings.

    Args:
        snapshot_holdings: [{"code": "601698", "name": "中国卫通", "quantity": 1100, "cost_price": 33.94}, ...]
        screenshot_holdings: [{"name": "中国卫通", "quantity": 1100, "cost_price": 34.15}, ...]
                              (may not have code, name-based matching)
    
    Returns:
        List of HoldingDiff with discrepancies highlighted
    """
    # Build lookup maps
    snap_by_code: dict[str, dict] = {h["code"]: h for h in snapshot_holdings}
    snap_by_name: dict[str, dict] = {h["name"]: h for h in snapshot_holdings}

    diffs: list[HoldingDiff] = []
    matched_snap_codes: set[str] = set()

    for sh in screenshot_holdings:
        name = sh.get("name", "")
        qty = sh.get("quantity", 0)
        cost = sh.get("cost_price")

        # Try to match by name first
        snap = snap_by_name.get(name)
        if not snap:
            # Try fuzzy match
            for sn, sv in snap_by_name.items():
                if name and sn and (name in sn or sn in name):
                    snap = sv
                    break

        if snap:
            code = snap["code"]
            matched_snap_codes.add(code)
            has_disc = (
                snap["quantity"] != qty
                or (cost is not None and snap["cost_price"] != cost)
            )
            diffs.append(HoldingDiff(
                code=code,
                name=name or snap["name"],
                snapshot_qty=snap["quantity"],
                screenshot_qty=qty,
                snapshot_cost=snap["cost_price"],
                screenshot_cost=cost,
                has_discrepancy=has_disc,
            ))
        else:
            diffs.append(HoldingDiff(
                code="??",
                name=name,
                snapshot_qty=0,
                screenshot_qty=qty,
                snapshot_cost=Decimal("0"),
                screenshot_cost=cost,
                has_discrepancy=True,
            ))

    # Add holdings in snapshot but not in screenshot
    for h in snapshot_holdings:
        if h["code"] not in matched_snap_codes:
            diffs.append(HoldingDiff(
                code=h["code"],
                name=h["name"],
                snapshot_qty=h["quantity"],
                screenshot_qty=0,
                snapshot_cost=h["cost_price"],
                screenshot_cost=None,
                has_discrepancy=True,
            ))

    return diffs


def render_reconciliation(
    snapshot_holdings: list[dict],
    screenshot_holdings: list[dict],
    snap_date: str = "",
    snap_assets: Decimal | None = None,
    snap_cash: Decimal | None = None,
) -> str:
    """Render full reconciliation output."""
    diffs = compare_holdings(snapshot_holdings, screenshot_holdings)

    lines = ["## 🔍 快照 vs 截图对账", ""]

    if snap_date:
        lines.append(f"快照日期: {snap_date}")
    if snap_assets is not None:
        lines.append(f"快照总资产: {snap_assets:.2f}")
    if snap_cash is not None:
        lines.append(f"快照现金: {snap_cash:.2f}")
    lines.append("")

    has_any = any(d.has_discrepancy for d in diffs)
    if has_any:
        lines.append("### ⚠️ 发现差异！")
        lines.append("")
    else:
        lines.append("### ✅ 快照与截图一致")
        lines.append("")

    lines.append("| 标的 | 快照股数 | 截图股数 | 快照成本 | 截图成本 | 状态 |")
    lines.append("|------|----------|----------|----------|----------|------|")

    for d in diffs:
        qty_snap = str(d.snapshot_qty)
        qty_scr = str(d.screenshot_qty) if d.screenshot_qty is not None else "—"
        cost_snap = _fmt_price(d.snapshot_cost)
        cost_scr = _fmt_price(d.screenshot_cost) if d.screenshot_cost is not None else "—"

        if d.has_discrepancy:
            if d.snapshot_qty == 0:
                status = "🆕 截图有、快照无"
            elif d.screenshot_qty == 0:
                status = "❌ 快照有、截图无"
            elif d.snapshot_qty != d.screenshot_qty:
                status = f"🔴 股数差 {d.screenshot_qty - d.snapshot_qty:+d}"
            else:
                status = "🟡 成本差"
        else:
            status = "✅ 一致"

        lines.append(f"| {d.name} | {qty_snap} | {qty_scr} | {cost_snap} | {cost_scr} | {status} |")

    lines.append("")
    if has_any:
        lines.append("### 📝 差异汇总")
        for d in diffs:
            if d.has_discrepancy:
                if d.snapshot_qty == 0:
                    lines.append(f"- **{d.name}**: 截图中有但快照里没有 → 需要加入快照")
                elif d.screenshot_qty == 0:
                    lines.append(f"- **{d.name}**: 快照中有但截图里没有 → 该票是否已卖出？需更新快照")
                elif d.snapshot_qty != d.screenshot_qty:
                    lines.append(f"- **{d.name}**: 股数不一致 → 快照{d.snapshot_qty}股 vs 截图{d.screenshot_qty}股")
                elif d.screenshot_cost is not None and d.snapshot_cost != d.screenshot_cost:
                    lines.append(f"- **{d.name}**: 成本不一致 → 快照{_fmt_price(d.snapshot_cost)} vs 截图{_fmt_price(d.screenshot_cost)}")

    return "\n".join(lines)
