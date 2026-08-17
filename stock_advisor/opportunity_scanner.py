"""Opportunity Scanner — proactively find buy candidates across broader market.

Unlike the passive monitoring daemon (which only watches existing holdings),
this module scans a wide universe of A-share stocks for technical setups
with high reward/risk ratios.

Strategy: screen by MA alignment + volume confirmation + trend health,
then rank by composite score. Designed to be run daily (morning or midday).

Usage:
    python3 -m stock_advisor.opportunity_scanner --config config.yaml --top 5
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .config import AppConfig, require_valid_config
from .models import EntryPlan, ExitPlanV2, StockQuote, TradeIdea
from .news import fetch_stock_news, is_important_announcement
from .portfolio import load_snapshot as load_portfolio_snapshot

logger = logging.getLogger(__name__)

POSITIVE_NEWS_KEYWORDS = (
    "中标", "订单", "合作", "回购", "增持", "预增", "业绩增长", "突破", "签约", "利好",
)

NEGATIVE_NEWS_KEYWORDS = (
    "减持", "处罚", "立案", "诉讼", "亏损", "预减", "风险", "停牌", "解禁", "下滑",
)

# ── Broad A-share universe (~200 stocks across major sectors) ──
# Sectors: 半导体/通信/军工/新能源/消费电子/医药/软件/金融
DEFAULT_UNIVERSE: list[dict[str, str]] = [
    # ── 半导体 (20) ──
    {"code": "002049", "name": "紫光国微"}, {"code": "002185", "name": "华天科技"},
    {"code": "002156", "name": "通富微电"}, {"code": "002371", "name": "北方华创"},
    {"code": "603986", "name": "兆易创新"}, {"code": "688981", "name": "中芯国际"},
    {"code": "300782", "name": "卓胜微"}, {"code": "688012", "name": "中微公司"},
    {"code": "002415", "name": "海康威视"}, {"code": "603501", "name": "韦尔股份"},
    {"code": "300661", "name": "圣邦股份"}, {"code": "688396", "name": "华润微"},
    {"code": "002916", "name": "深南电路"}, {"code": "603160", "name": "汇顶科技"},
    {"code": "300223", "name": "北京君正"}, {"code": "688256", "name": "寒武纪"},
    {"code": "002409", "name": "雅克科技"}, {"code": "603290", "name": "斯达半导"},
    {"code": "688536", "name": "思瑞浦"}, {"code": "300474", "name": "景嘉微"},
    # ── 通信/5G (15) ──
    {"code": "601698", "name": "中国卫通"}, {"code": "000063", "name": "中兴通讯"},
    {"code": "600941", "name": "中国移动"}, {"code": "600050", "name": "中国联通"},
    {"code": "601728", "name": "中国电信"}, {"code": "300394", "name": "天孚通信"},
    {"code": "300502", "name": "新易盛"}, {"code": "300308", "name": "中际旭创"},
    {"code": "002544", "name": "普天科技"}, {"code": "603236", "name": "移远通信"},
    {"code": "300638", "name": "广和通"}, {"code": "002396", "name": "星网锐捷"},
    {"code": "300628", "name": "亿联网络"}, {"code": "002583", "name": "海能达"},
    {"code": "600198", "name": "大唐电信"},
    # ── 计算机/软件 (20) ──
    {"code": "002439", "name": "启明星辰"}, {"code": "002230", "name": "科大讯飞"},
    {"code": "600536", "name": "中国软件"}, {"code": "300454", "name": "深信服"},
    {"code": "688111", "name": "金山办公"}, {"code": "002410", "name": "广联达"},
    {"code": "300033", "name": "同花顺"}, {"code": "300059", "name": "东方财富"},
    {"code": "600570", "name": "恒生电子"}, {"code": "002405", "name": "四维图新"},
    {"code": "300369", "name": "绿盟科技"}, {"code": "002268", "name": "卫士通"},
    {"code": "300188", "name": "美亚柏科"}, {"code": "688561", "name": "奇安信"},
    {"code": "300379", "name": "东方通"}, {"code": "002065", "name": "东华软件"},
    {"code": "600588", "name": "用友网络"}, {"code": "002368", "name": "太极股份"},
    {"code": "300674", "name": "宇信科技"}, {"code": "603859", "name": "能科科技"},
    # ── 军工/航天 (15) ──
    {"code": "600893", "name": "航发动力"}, {"code": "002025", "name": "航天电器"},
    {"code": "600760", "name": "中航沈飞"}, {"code": "600118", "name": "中国卫星"},
    {"code": "600879", "name": "航天电子"}, {"code": "600391", "name": "航发科技"},
    {"code": "002013", "name": "中航机电"}, {"code": "600038", "name": "中直股份"},
    {"code": "300114", "name": "中航电测"}, {"code": "002465", "name": "海格通信"},
    {"code": "300045", "name": "华力创通"}, {"code": "600990", "name": "四创电子"},
    {"code": "300762", "name": "上海瀚讯"}, {"code": "002151", "name": "北斗星通"},
    {"code": "300101", "name": "振芯科技"},
    # ── 新能源/光伏 (15) ──
    {"code": "300750", "name": "宁德时代"}, {"code": "601012", "name": "隆基绿能"},
    {"code": "002129", "name": "中环股份"}, {"code": "600438", "name": "通威股份"},
    {"code": "002459", "name": "晶澳科技"}, {"code": "300274", "name": "阳光电源"},
    {"code": "601615", "name": "明阳智能"}, {"code": "002074", "name": "国轩高科"},
    {"code": "300014", "name": "亿纬锂能"}, {"code": "002340", "name": "格林美"},
    {"code": "603799", "name": "华友钴业"}, {"code": "002460", "name": "赣锋锂业"},
    {"code": "300450", "name": "先导智能"}, {"code": "002812", "name": "恩捷股份"},
    {"code": "688005", "name": "容百科技"},
    # ── 消费电子 (10) ──
    {"code": "002475", "name": "立讯精密"}, {"code": "601138", "name": "工业富联"},
    {"code": "002241", "name": "歌尔股份"}, {"code": "300136", "name": "信维通信"},
    {"code": "002600", "name": "领益智造"}, {"code": "300115", "name": "长盈精密"},
    {"code": "002456", "name": "欧菲光"}, {"code": "300433", "name": "蓝思科技"},
    {"code": "002273", "name": "水晶光电"}, {"code": "300709", "name": "精研科技"},
    # ── 医药 (10) ──
    {"code": "300760", "name": "迈瑞医疗"}, {"code": "002821", "name": "凯莱英"},
    {"code": "300759", "name": "康龙化成"}, {"code": "300347", "name": "泰格医药"},
    {"code": "002007", "name": "华兰生物"}, {"code": "300122", "name": "智飞生物"},
    {"code": "600276", "name": "恒瑞医药"}, {"code": "000661", "name": "长春高新"},
    {"code": "002317", "name": "众生药业"}, {"code": "688180", "name": "君实生物"},
    # ── 大金融 (10) ──
    {"code": "601318", "name": "中国平安"}, {"code": "600036", "name": "招商银行"},
    {"code": "601166", "name": "兴业银行"}, {"code": "000001", "name": "平安银行"},
    {"code": "600030", "name": "中信证券"}, {"code": "300059", "name": "东方财富"},
    {"code": "601688", "name": "华泰证券"}, {"code": "601211", "name": "国泰君安"},
    {"code": "601628", "name": "中国人寿"}, {"code": "600519", "name": "贵州茅台"},
]


@dataclass(slots=True)
class Candidate:
    code: str
    name: str
    current_price: Decimal = Decimal("0")
    change_pct: Decimal = Decimal("0")
    volume_ratio: Decimal = Decimal("0")
    ma5: Decimal = Decimal("0")
    ma15: Decimal = Decimal("0")
    ma60: Decimal = Decimal("0")
    rsi14: Decimal = Decimal("0")
    ma_alignment: int = 0  # -4 to +4
    composite_score: Decimal = Decimal("0")
    flags: list[str] = field(default_factory=list)
    sector: str = ""


@dataclass(slots=True)
class SuggestedPosition:
    label: str
    quantity: int
    max_budget: Decimal
    affordable_lots: int


@dataclass(slots=True)
class ExitPlan:
    action: str
    quantity: int
    target_price: Decimal
    fallback_price: Decimal
    reason: str


def _tencent_symbol(code: str) -> str:
    """Convert 6-digit code to Tencent format: sh600036 or sz000001."""
    if code.startswith(("6", "9")):
        return f"sh{code}"
    return f"sz{code}"


def _fetch_quotes_tencent(codes: list[str]) -> dict[str, dict[str, Any]]:
    """Batch-fetch quotes via Tencent API. Returns {code: {fields}}."""
    if not codes:
        return {}

    symbols = [_tencent_symbol(c) for c in codes]
    url = f"http://qt.gtimg.cn/q={','.join(symbols)}"

    try:
        from .platform_compat import http_get_bytes
        data = http_get_bytes(url, timeout=12)
        if not data:
            logger.warning("Tencent quote fetch failed: empty response")
            return {}
        # Tencent API returns GBK-encoded content
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("gbk", errors="replace")
        return _parse_tencent_output(text, codes)
    except Exception as exc:
        logger.warning("stock_advisor/opportunity_scanner.py:_fetch_quotes_tencent failed: %s", exc)
        logger.exception("Tencent quote fetch exception")
        return {}


def _parse_tencent_output(text: str, codes: list[str]) -> dict[str, dict[str, Any]]:
    """Parse Tencent's semicolon-delimited output like v_sh600036="1~平安银行~..."""
    results: dict[str, dict[str, Any]] = {}
    code_map = {_tencent_symbol(c): c for c in codes}

    for line in text.split("\n"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        try:
            var_name, _, value = line.partition("=")
            # var_name like 'v_sh600036'
            symbol = var_name.replace("v_", "").strip('"')
            if symbol not in code_map:
                continue
            # Value is quoted like "1~平安银行~..."
            raw = value.strip().strip('"').strip(";")
            if not raw:
                continue
            parts = raw.split("~")
            if len(parts) < 50:
                continue
            code = code_map[symbol]
            results[code] = {
                "name": parts[1],
                "current_price": _safe_decimal(parts[3]),
                "prev_close": _safe_decimal(parts[4]),
                "open_price": _safe_decimal(parts[5]),
                "volume": _safe_decimal(parts[6]),  # 手
                "high": _safe_decimal(parts[33]),
                "low": _safe_decimal(parts[34]),
                "change_pct": _safe_decimal(parts[32]),
                "turnover_rate": _safe_decimal(parts[38]),
                "pe": _safe_decimal(parts[39]),
            }
        except Exception as exc:
            logger.warning("stock_advisor/opportunity_scanner.py:_parse_tencent_output failed: %s", exc)
            continue
    return results


def _safe_decimal(s: str) -> Decimal:
    try:
        return Decimal(s.strip() or "0")
    except Exception as exc:
        logger.warning("stock_advisor/opportunity_scanner.py:_safe_decimal failed: %s", exc)
        return Decimal("0")


def _simple_ma(prices: list[Decimal], n: int) -> Decimal:
    """Simple moving average of last n prices."""
    if len(prices) < n or n <= 0:
        return Decimal("0")
    total = sum(prices[-n:])
    return Decimal(str(total / n)).quantize(Decimal("0.01"))


def _build_news_quote(code: str, name: str, payload: dict[str, Any]) -> StockQuote:
    symbol = _tencent_symbol(code)
    now = datetime.now()
    current = payload.get("current_price", Decimal("0"))
    prev_close = payload.get("prev_close", current)
    open_price = payload.get("open_price", current)
    high = payload.get("high", current)
    low = payload.get("low", current)
    volume_hands = payload.get("volume", Decimal("0"))
    volume_shares = volume_hands * Decimal("100")
    turnover_yuan = current * volume_shares
    return StockQuote(
        provider="tencent",
        symbol=symbol,
        code=code,
        name=name,
        current_price=current,
        open_price=open_price,
        previous_close=prev_close,
        high_price=high,
        low_price=low,
        change_amount=(current - prev_close).quantize(Decimal("0.01")) if prev_close > 0 else Decimal("0"),
        change_percent=payload.get("change_pct", Decimal("0")),
        volume_shares=volume_shares,
        turnover_yuan=turnover_yuan.quantize(Decimal("0.01")) if turnover_yuan > 0 else Decimal("0"),
        quote_time=now,
        raw_payload="",
    )


def _score_recent_news(code: str, name: str, payload: dict[str, Any]) -> tuple[Decimal, list[str]]:
    """Reward fresh constructive headlines, punish obvious negatives."""
    try:
        quote = _build_news_quote(code, name, payload)
        items = fetch_stock_news(quote, limit=3)
    except Exception as exc:
        logger.warning("stock_advisor/opportunity_scanner.py:_score_recent_news failed: %s", exc)
        logger.exception("News scoring failed for %s", code)
        return Decimal("0"), []

    boost = Decimal("0")
    flags: list[str] = []
    seen_labels: set[str] = set()
    for item in items:
        title = item.title or ""
        positive_hits = sum(1 for kw in POSITIVE_NEWS_KEYWORDS if kw in title)
        negative_hits = sum(1 for kw in NEGATIVE_NEWS_KEYWORDS if kw in title)
        important = is_important_announcement(title)
        if negative_hits > 0:
            penalty = Decimal("4") if important else Decimal("2")
            boost -= penalty
            if "news_negative" not in seen_labels:
                flags.append("📰 近期有利空/风险公告")
                seen_labels.add("news_negative")
            continue
        if positive_hits > 0:
            reward = Decimal("4") if important else Decimal("2")
            boost += reward
            if "news_positive" not in seen_labels:
                flags.append("📰 近期有催化/利好新闻")
                seen_labels.add("news_positive")

    boost = max(Decimal("-6"), min(boost, Decimal("6")))
    return boost, flags


def _simple_rsi(prices: list[Decimal], n: int = 14) -> Decimal:
    """Simple RSI calculation."""
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


def _fetch_daily_closes(code: str, ndays: int = 60) -> list[Decimal]:
    """Fetch daily closing prices from Tencent K-line API."""
    symbol = _tencent_symbol(code)
    url = (
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={symbol},day,,,{ndays},qfq"
    )
    try:
        from .platform_compat import http_get_text
        text = http_get_text(url, timeout=10, encoding="utf-8")
        if not text:
            return []
        data = json.loads(text)
        rows = data.get("data", {}).get(symbol, {}).get("qfqday", [])
        if not rows:
            rows = data.get("data", {}).get(symbol, {}).get("day", [])
        # Each row: [date, open, close, high, low, volume]
        return [_safe_decimal(r[2]) for r in rows if len(r) >= 3]
    except Exception as exc:
        logger.warning("stock_advisor/opportunity_scanner.py:_fetch_daily_closes failed: %s", exc)
        return []


def _fetch_daily_closes_batch(codes: list[str], ndays: int = 60) -> dict[str, list[Decimal]]:
    """Batch-fetch daily closes for multiple codes (one HTTP call per code, serial)."""
    results: dict[str, list[Decimal]] = {}
    for code in codes:
        closes = _fetch_daily_closes(code, ndays)
        if closes:
            results[code] = closes
    return results


def _ma_alignment_score(price: Decimal, ma5: Decimal, ma15: Decimal, ma60: Decimal) -> int:
    """MA stack score: +n bullish, -n bearish, 0 neutral."""
    levels = [v for v in [price, ma5, ma15, ma60] if v > 0]
    if len(levels) < 2:
        return 0
    bull = sum(1 for a, b in zip(levels, levels[1:]) if a > b)
    bear = sum(1 for a, b in zip(levels, levels[1:]) if a < b)
    if bull == len(levels) - 1:
        return bull
    if bear == len(levels) - 1:
        return -bear
    return bull - bear


def scan(
    config_path: str | Path = "config.yaml",
    *,
    universe: list[dict[str, str]] | None = None,
    top_n: int = 10,
    exclude_codes: set[str] | None = None,
    max_change_pct: float = 5.0,
) -> list[Candidate]:
    """Scan the universe for buy-worthy candidates.

    Two-phase approach:
      1. Quick screen with real-time data (Tencent batch API, one call)
      2. Deep score with daily history (only for top ~30 passers)

    Args:
        config_path: Path to config.yaml for sector data
        universe: Custom stock universe (default: DEFAULT_UNIVERSE)
        top_n: Return top N candidates
        exclude_codes: Codes to exclude (e.g. existing holdings)
        max_change_pct: Maximum daily change to consider (anti-chase)
    """
    config = require_valid_config(config_path)
    stocks = universe or DEFAULT_UNIVERSE
    exclude = exclude_codes or set()
    codes = [s["code"] for s in stocks if s["code"] not in exclude]
    min_candidate_score = max(Decimal("84"), Decimal(str(config.monitor.decision_thresholds.buy_score)) + Decimal("2"))
    result_cap = min(top_n, 2)

    # ═══════ Phase 1: Quick screen with real-time data ═══════
    quotes = _fetch_quotes_tencent(codes)
    if not quotes:
        logger.warning("No quotes returned from Tencent API")
        return []

    passers: list[dict[str, Any]] = []
    for stock in stocks:
        code = stock["code"]
        if code not in quotes:
            continue
        q = quotes[code]
        price = q.get("current_price", Decimal("0"))
        if price <= 0:
            continue
        change_pct = q.get("change_pct", Decimal("0"))

        # Quick filters
        if float(change_pct) > max_change_pct:
            continue
        if float(price) < 5 or float(price) > 60:
            continue
        vol = float(q.get("volume", 0))
        if vol < 500000:  # At least 500K shares traded
            continue

        passers.append({"code": code, "name": q.get("name", stock.get("name", code)),
                        "price": price, "change_pct": change_pct,
                        "volume": vol, "turnover": float(q.get("turnover_rate", 0))})

    # Sort passers by volume * price (active stocks first), take top ~max(30, top_n*3)
    passers.sort(key=lambda x: x["volume"] * float(x["price"]), reverse=True)
    deep_candidates = max(30, top_n * 3)
    passers = passers[:deep_candidates]

    # ═══════ Phase 2: Deep score with daily history ═══════
    # Batch-fetch daily closes for all passers
    passer_codes = [p["code"] for p in passers]
    daily_data = _fetch_daily_closes_batch(passer_codes, ndays=60)

    # ─── Fetch sector strength once (outside loop) ───
    sector_boards = None
    try:
        from .sector_strength import fetch_sector_boards
        sector_boards = fetch_sector_boards(top_n=60)
    except Exception as exc:
        logger.warning("stock_advisor/opportunity_scanner.py:scan failed: %s", exc)

    candidates: list[Candidate] = []

    for p in passers:
        code = p["code"]
        price = p["price"]
        change_pct = p["change_pct"]

        closes = daily_data.get(code, [])
        if len(closes) < 20:
            continue

        ma5 = _simple_ma(closes, 5)
        ma15 = _simple_ma(closes, 15)
        ma60 = _simple_ma(closes, 60)
        rsi14 = _simple_rsi(closes, 14)
        ma_align = _ma_alignment_score(price, ma5, ma15, ma60)

        # ── Scoring ──
        score = Decimal("50")
        flags: list[str] = []

        # MA alignment
        if ma_align >= 3:
            score += Decimal("20"); flags.append("🟢 多头排列完整")
        elif ma_align >= 1:
            score += Decimal("12"); flags.append("🟡 偏多头")
        elif ma_align <= -3:
            score -= Decimal("25"); flags.append("🔴 空头排列")
            continue  # Skip deeply bearish stocks
        elif ma_align <= -1:
            score -= Decimal("10"); flags.append("🟠 偏空头")

        # MA20 mean reversion
        if ma15 > 0:
            bias = float((price - ma15) / ma15 * 100)
            if -3 < bias < 1:
                score += Decimal("12"); flags.append(f"📏 贴近均线({bias:+.1f}%)")
            elif -1 < bias < 3:
                score += Decimal("8"); flags.append(f"📏 均线附近({bias:+.1f}%)")
            elif bias > 8:
                score -= Decimal("15"); flags.append(f"⚠️ 偏离过远({bias:+.1f}%)")

        # RSI
        if 40 <= float(rsi14) <= 65:
            score += Decimal("8"); flags.append(f"💪 RSI健康({rsi14})")
        elif float(rsi14) < 35:
            score += Decimal("10"); flags.append(f"🔔 RSI超卖({rsi14})反弹机会")
        elif float(rsi14) > 75:
            score -= Decimal("8"); flags.append(f"🔥 RSI过热({rsi14})")

        # Volume
        if float(change_pct) > 0 and p["turnover"] > 1.0:
            score += Decimal("5"); flags.append("📊 活跃放量")
        elif float(change_pct) < 0 and p["turnover"] > 2.0:
            score -= Decimal("3")  # Heavy selling

        # News catalyst overlay
        news_boost, news_flags = _score_recent_news(code, p["name"], quotes.get(code, {}))
        if news_boost != 0:
            score += news_boost
        flags.extend(news_flags)

        # Sector bonus (use pre-fetched boards)
        if sector_boards:
            try:
                from .sector_strength import compute_sector_score_boost
                boost = compute_sector_score_boost(sector_boards, _tencent_symbol(code))
                if boost != 0:
                    score += Decimal(str(boost))
                    if boost > 0:
                        flags.append(f"📈 板块走强(+{boost})")
            except Exception as exc:
                logger.warning("stock_advisor/opportunity_scanner.py:scan failed: %s", exc)

        score = max(Decimal("0"), min(score, Decimal("100")))
        if score < min_candidate_score:
            continue

        candidates.append(Candidate(
            code=code, name=p["name"],
            current_price=price, change_pct=change_pct,
            volume_ratio=Decimal("1.0"), ma5=ma5, ma15=ma15, ma60=ma60,
            rsi14=rsi14, ma_alignment=ma_align,
            composite_score=score, flags=flags,
            sector=_guess_sector(code, p["name"]),
        ))

    candidates.sort(
        key=lambda c: (
            float(c.composite_score),
            1 if any("催化/利好" in flag for flag in c.flags) else 0,
            -abs(float(c.change_pct)),
        ),
        reverse=True,
    )
    return candidates[:result_cap]


def _guess_sector(code: str, name: str) -> str:
    """Guess sector from stock code or name."""
    sector_map = {
        "601698": "通信", "000063": "通信", "600941": "通信", "600050": "通信",
        "002439": "网安", "002230": "AI",
        "002049": "半导体", "002185": "半导体", "002156": "半导体", "002371": "半导体",
        "300750": "新能源", "601012": "光伏", "002129": "光伏",
        "002475": "消费电子", "601138": "消费电子", "002241": "消费电子",
        "600893": "军工", "002025": "军工", "600760": "军工",
        "601318": "金融", "600036": "金融", "600030": "金融",
        "300760": "医药", "600276": "医药",
    }
    return sector_map.get(code, "其他")


def suggest_position(config: AppConfig, current_price: Decimal, score: Decimal | None = None) -> SuggestedPosition:
    """Size proposed positions from actual cash and configured risk controls."""
    lot_size = 100
    if current_price <= 0:
        return SuggestedPosition(label="观察", quantity=0, max_budget=Decimal("0"), affordable_lots=0)

    snapshot = None
    try:
        if config.snapshot_path.exists():
            snapshot = load_portfolio_snapshot(config.snapshot_path)
    except Exception as exc:
        logger.warning("stock_advisor/opportunity_scanner.py:suggest_position failed: %s", exc)
        logger.exception("Failed to load portfolio snapshot for position sizing")

    total_assets = getattr(snapshot, "total_assets", Decimal("0")) or Decimal("0")
    cash = getattr(snapshot, "cash", Decimal("0")) or Decimal("0")
    reserve_ratio = Decimal(str(config.monitor.risk_controls.min_cash_pct)) / Decimal("100")
    reserve_cash = (total_assets * reserve_ratio).quantize(Decimal("0.01")) if total_assets > 0 else Decimal("0")
    deployable_cash = max(Decimal("0"), cash - reserve_cash)
    lot_cost = (current_price * lot_size).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    affordable_lots = int(deployable_cash // lot_cost) if lot_cost > 0 else 0

    tiers = config.monitor.position_tiers or []
    if score is None:
        selected_tier = tiers[0] if tiers else None
    else:
        selected_tier = None
        for tier in tiers:
            if float(score) >= float(tier.score_min):
                selected_tier = tier
        if selected_tier is None and tiers:
            selected_tier = tiers[0]

    if selected_tier is None:
        max_budget = deployable_cash
        label = "试探仓"
    else:
        max_budget = (total_assets * Decimal(str(selected_tier.pct_of_assets))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        ) if total_assets > 0 else deployable_cash
        label = selected_tier.label
    tier_lots = int(max_budget // lot_cost) if lot_cost > 0 else 0
    quantity = min(affordable_lots, tier_lots) * lot_size

    return SuggestedPosition(
        label=label if quantity > 0 else "观察",
        quantity=quantity,
        max_budget=max_budget,
        affordable_lots=affordable_lots,
    )


def can_afford_candidate(config: AppConfig, current_price: Decimal, score: Decimal | None = None) -> bool:
    return suggest_position(config, current_price, score).quantity > 0


def build_exit_plan(holding, *, max_single_position_pct: float) -> ExitPlan:
    cost = getattr(holding, "cost_price", Decimal("0")) or Decimal("0")
    current = getattr(holding, "current_price", Decimal("0")) or Decimal("0")
    quantity = int(getattr(holding, "quantity", 0) or 0)
    if cost <= 0 or current <= 0 or quantity <= 0:
        return ExitPlan("观察", 0, Decimal("0"), Decimal("0"), "持仓数据不完整")

    pnl_pct = ((current - cost) / cost * Decimal("100")).quantize(Decimal("0.01"))
    reduce_qty = max(100, (quantity // 4 // 100) * 100) if quantity >= 400 else 100
    reduce_qty = min(reduce_qty, quantity)
    target_price = current
    fallback_price = (current * Decimal("0.97")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if pnl_pct >= Decimal("8"):
        target_price = (current * Decimal("1.02")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return ExitPlan("止盈减仓", reduce_qty, target_price, fallback_price, f"浮盈{pnl_pct}%先落袋，再用剩余仓位跟踪趋势")
    if pnl_pct >= Decimal("3"):
        target_price = (current * Decimal("1.015")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return ExitPlan("逢高落袋", reduce_qty, target_price, fallback_price, f"已有浮盈{pnl_pct}%，先锁一部分利润")
    if pnl_pct <= Decimal("-10"):
        target_price = (current * Decimal("1.02")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        fallback_price = (current * Decimal("0.98")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return ExitPlan("卖点计划", min(max(200, reduce_qty), quantity), target_price, fallback_price, f"浮亏{pnl_pct}%过深，反弹到计划卖点先减仓，跌破防守位执行止损")
    if pnl_pct < Decimal("0"):
        target_price = (current * Decimal("1.03")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return ExitPlan("减仓计划", reduce_qty, target_price, fallback_price, f"浮亏{pnl_pct}%先按计划卖点降仓位")
    return ExitPlan("持有观察", 0, current, fallback_price, f"盈亏{pnl_pct}%不极端，暂不主动卖")


def build_trade_idea(candidate: Candidate, config: AppConfig) -> TradeIdea:
    position = suggest_position(config, candidate.current_price, candidate.composite_score)
    entry_plan = _build_entry_plan(candidate)
    exit_plan = _build_exit_plan_v2(candidate)
    return TradeIdea(
        code=candidate.code,
        name=candidate.name,
        sector=candidate.sector,
        score=candidate.composite_score,
        current_price=candidate.current_price,
        position_label=position.label,
        suggested_quantity=position.quantity,
        entry_plan=entry_plan,
        exit_plan=exit_plan,
        thesis=candidate.flags[:4],
    )


def build_trade_ideas(candidates: list[Candidate], config: AppConfig) -> list[TradeIdea]:
    return [build_trade_idea(candidate, config) for candidate in candidates]


def _build_entry_plan(candidate: Candidate) -> EntryPlan:
    price = candidate.current_price
    pullback_low = (price * Decimal("0.990")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    pullback_high = (price * Decimal("0.997")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cancel_below = (price * Decimal("0.972")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    chase_above = (price * Decimal("1.012")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    trigger_price = (price * Decimal("1.006")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    has_positive_news = any("催化/利好" in flag for flag in candidate.flags)

    if (
        candidate.composite_score >= Decimal("88")
        and candidate.ma_alignment >= 3
        and candidate.change_pct <= Decimal("1.50")
        and Decimal("45") <= candidate.rsi14 <= Decimal("62")
    ):
        entry_type = "回踩确认买"
        note = f"只等回踩 {pullback_low}-{pullback_high} 企稳再买，没回踩到位就放弃当天追单。"
    elif (
        candidate.composite_score >= Decimal("86")
        and candidate.ma_alignment >= 2
        and candidate.rsi14 <= Decimal("33")
        and candidate.change_pct >= Decimal("-1.50")
    ):
        entry_type = "强势低吸买"
        note = f"仅限强趋势里的超卖修复，先小仓低吸；跌破 {cancel_below} 计划立即作废。"
    else:
        entry_type = "突破确认买"
        if has_positive_news:
            note = f"有催化但也不追高，必须放量站上 {trigger_price} 再买；若直接冲过 {chase_above} 当天放弃。"
        else:
            note = f"无催化不抢突破，必须站上 {trigger_price} 且别追过 {chase_above}；宁可错过，不做盘中乱追。"

    return EntryPlan(
        entry_type=entry_type,
        trigger_price=trigger_price,
        buy_zone_low=pullback_low,
        buy_zone_high=pullback_high,
        cancel_below=cancel_below,
        chase_above=chase_above,
        note=note,
    )


def _build_exit_plan_v2(candidate: Candidate) -> ExitPlanV2:
    price = candidate.current_price
    stop_loss = (price * Decimal("0.93")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if candidate.composite_score >= Decimal("90"):
        first_take_profit = (price * Decimal("1.07")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        final_take_profit = (price * Decimal("1.15")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        trailing_pct = Decimal("4.50")
        note = f"高分强票不急着卖飞，先看 {first_take_profit} 附近减 1/3，剩余仓位交给 {trailing_pct}% 峰值回撤移动止盈。"
    elif candidate.composite_score >= Decimal("86"):
        first_take_profit = (price * Decimal("1.06")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        final_take_profit = (price * Decimal("1.13")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        trailing_pct = Decimal("4.00")
        note = f"先看 {first_take_profit} 附近主动落袋 1/3，强势再看 {final_take_profit}，剩余仓位用 {trailing_pct}% 峰值回撤保护。"
    else:
        first_take_profit = (price * Decimal("1.05")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        final_take_profit = (price * Decimal("1.10")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        trailing_pct = Decimal("3.50")
        note = f"普通机会先看 {first_take_profit} 附近兑现，再留一部分仓位观察是否走成趋势。"
    return ExitPlanV2(
        stop_loss=stop_loss,
        first_take_profit=first_take_profit,
        final_take_profit=final_take_profit,
        trailing_take_profit_pct=trailing_pct,
        note=note,
    )



def render_candidates(candidates: list[Candidate], *, config: AppConfig | None = None) -> str:
    """Format candidates as markdown for display."""
    if config is not None:
        candidates = [c for c in candidates if can_afford_candidate(config, c.current_price, c.composite_score)]
    if not candidates:
        return "📭 本轮扫描未发现符合条件的候选标的。"

    lines = [f"## 🔍 机会扫描结果（Top {len(candidates)}）\n"]
    lines.append("| # | 标的 | 现价 | 涨跌 | 评分 | MA排列 | 信号 |")
    lines.append("|---|------|------|------|------|--------|------|")

    for i, c in enumerate(candidates, 1):
        flags_str = " ".join(c.flags[:3])
        lines.append(
            f"| {i} | {c.name}({c.code}) | {c.current_price} | "
            f"{c.change_pct:+.2f}% | {c.composite_score} | "
            f"{'🟢' if c.ma_alignment >= 1 else '🔴' if c.ma_alignment <= -1 else '⚪'} | "
            f"{flags_str} |"
        )

    lines.append("")
    lines.append("### 💡 重点推荐")
    for i, c in enumerate(candidates[:3], 1):
        lines.append(f"**#{i} {c.name}({c.code})** — 评分{c.composite_score}")
        if c.flags:
            lines.append(f"> {' · '.join(c.flags[:4])}")
        position = suggest_position(config, c.current_price, c.composite_score) if config else None
        if position and position.quantity > 0:
            advice = f"{position.label}{position.quantity}股"
        else:
            advice = "现金不足，先观察"
        lines.append(f"> 🎯 建议：{advice} @ {c.current_price}，止损{c.current_price * Decimal('0.93'):.2f}")
        lines.append("")

    return "\n".join(lines)


def render_trade_ideas(ideas: list[TradeIdea]) -> str:
    if not ideas:
        return "📭 本轮没有可直接下手的交易机会。"

    summary_lines = ["## 今日结论"]
    for idx, idea in enumerate(ideas, start=1):
        qty_text = f"{idea.position_label}{idea.suggested_quantity}股" if idea.suggested_quantity > 0 else "观察，不下单"
        summary_lines.append(
            f"{idx}. {idea.name}({idea.code})：{idea.entry_plan.entry_type}，"
            f"买点 {idea.entry_plan.buy_zone_low}-{idea.entry_plan.buy_zone_high} / {idea.entry_plan.trigger_price}，"
            f"止损 {idea.exit_plan.stop_loss}，先看 {idea.exit_plan.first_take_profit}，{qty_text}"
        )

    lines = summary_lines + ["", "## 逐条展开"]
    for idx, idea in enumerate(ideas, start=1):
        lines.append(f"\n### {idx}. {idea.name}({idea.code}) | {idea.sector} | 评分 {idea.score}")
        qty_text = f"{idea.position_label}{idea.suggested_quantity}股" if idea.suggested_quantity > 0 else "观察，当前不下单"
        lines.append(f"买什么：{idea.name}，建议仓位 {qty_text}")
        lines.append(
            f"什么时候买：{idea.entry_plan.entry_type}；先看 {idea.entry_plan.buy_zone_low}-{idea.entry_plan.buy_zone_high}；"
            f"确认突破看 {idea.entry_plan.trigger_price}；跌破 {idea.entry_plan.cancel_below} 放弃；高于 {idea.entry_plan.chase_above} 不追。"
        )
        lines.append(
            f"什么时候卖：先止损 {idea.exit_plan.stop_loss}；再看 {idea.exit_plan.first_take_profit}；"
            f"强势才看 {idea.exit_plan.final_take_profit}；剩余仓位用 {idea.exit_plan.trailing_take_profit_pct}% 移动止盈。"
        )
        if idea.thesis:
            lines.append(f"为什么是它：{'；'.join(idea.thesis)}")
        lines.append(f"备注：{idea.entry_plan.note} {idea.exit_plan.note}")
    return "\n".join(lines)


def run_cli():
    """CLI entry point for opportunity scanning."""
    import argparse
    parser = argparse.ArgumentParser(description="全市场机会扫描")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--top", type=int, default=10, help="返回Top N")
    parser.add_argument("--max-chg", type=float, default=5.0, help="最大涨幅过滤(%)")
    parser.add_argument("--exclude", nargs="*", default=None, help="排除代码")
    args = parser.parse_args()

    exclude = set(args.exclude) if args.exclude else set()
    config = require_valid_config(args.config)
    results = scan(
        config_path=args.config,
        top_n=args.top,
        max_change_pct=args.max_chg,
        exclude_codes=exclude,
    )
    print(render_trade_ideas(build_trade_ideas(results, config)))


if __name__ == "__main__":
    run_cli()
