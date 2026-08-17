#!/usr/bin/env python3
"""Bridge validator — PnL review + trend + intraday analysis before outbox delivery.

Reads data/outbox.jsonl (structured JSON), fetches daily K-line from Tencent
and minute data from market.db for multi-layered trend context.

Rules:
  1. Selling winner (>+3%): BLOCK unless trend turning down
     (MA5<MA20+declining vol) OR close<VWAP+tail down (momentum exhaustion)
  2. Selling small loser (-5% to +3%): BLOCK unless:
     - 缩量阴跌: vol concentrated at open + tail down + below VWAP
     - 弱势收盘: close in bottom 1/3 of range + tail down + ≤VWAP
     - 5+ consecutive decline days (any volume)
     - 3+ decline days with loss >2%
     BUT block if 放量探底 (vol at close + tail up + above VWAP → reversal)
  3. Buying deep loser (<-20%): BLOCK unless reversal signal
     (volume spike ≥2x avg + single-day >+5% gain)
  4. Zero-hold sell: BLOCK
  5. Briefing/review: always PASS

Usage:
  python3 -m stock_advisor.bridge_validator --mode validate   # validate & output
  python3 -m stock_advisor.bridge_validator --mode consume    # mark sent after delivery
"""

import argparse
import hashlib
import json
import logging
import os
import re
import time

logger = logging.getLogger(__name__)
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import requests

from stock_advisor.config import load_config
from stock_advisor.market_hours import is_a_share_trading_time, MARKET_TZ

REPO = Path(__file__).resolve().parent.parent
OUTBOX_PATH = REPO / "data" / "outbox.jsonl"
SNAPSHOT_PATH = REPO / "portfolio-snapshot.json"
SKIPPED_LOG = REPO / "data" / "bridge_skipped.log"
STATE_PATH = REPO / "data" / "bridge_validator_state.json"
DB_PATH = REPO / "data" / "market.db"
BLOCK_COOLDOWN_PATH = REPO / "data" / "bridge_block_cooldown.json"
TRIGGER_COOLDOWN_PATH = REPO / "data" / "bridge_trigger_cooldown.json"
TRIGGER_ALERT_COOLDOWN = 300  # 5 min between same trigger alerts
MAX_TRIGGER_FIRINGS = 3  # Auto-disable after N alerts
MAX_NOTE_LENGTH = 60       # Truncate verbose notes to prevent Feishu DM truncation
MAX_ALERTS_PER_RUN = 2     # Max trigger alerts per bridge tick

# ── Anomaly detection ──
ANOMALY_COOLDOWN_PATH = REPO / "data" / "bridge_anomaly_cooldown.json"


def _msg_hash(msg: str) -> str:
    """Stable hash of a message string, consistent across processes."""
    return hashlib.sha256(msg.encode("utf-8")).hexdigest()


def _item_hash(item: dict) -> str:
    """Stable hash of an outbox item, including created_at + title + message for uniqueness."""
    key = json.dumps({
        "created_at": item.get("created_at", ""),
        "title": item.get("title", ""),
        "message": item.get("message", ""),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
ANOMALY_COOLDOWN_SECONDS = 600      # 10 min between same anomaly type+code
FUND_OUTFLOW_THRESHOLD_YI = -0.3    # 主力净流出 > 3000万 → 警报
SUDDEN_DROP_THRESHOLD_PCT = -2.0    # 日内跌超 2% → 警报
SECTOR_SURGE_THRESHOLD = 3.0        # 板块涨超 3% → 检测背离
SECTOR_LAG_GAP = 5.0                # 个股跑输板块 > 5个百分点 → 警报
MAX_ANOMALIES_PER_RUN = 1           # Max anomaly alerts per bridge tick

_KLINE_CACHE: dict[str, list[dict]] = {}
_INTRADAY_CACHE: dict[str, dict] = {}
BLOCK_COOLDOWN_SECONDS = 300  # 5 minutes
_FUND_FLOW_CACHE: dict[str, tuple[float, str]] = {}  # code -> (ts, hint_str)


# ──────────────────────────────────────────────
#  K-line fetching (Tencent API)
# ──────────────────────────────────────────────

def _code_to_prefix(code: str) -> str:
    return "sh" + code if code.startswith("6") else "sz" + code


def fetch_daily_kline(code: str, ndays: int = 15) -> list[dict]:
    """Fetch daily K-line from Tencent. Returns list of {date, open, close, high, low, volume}."""
    if code in _KLINE_CACHE:
        return _KLINE_CACHE[code]

    prefix = _code_to_prefix(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix},day,,,{ndays},qfq"
    try:
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        raw = data.get("data", {}).get(prefix, {}).get("day", []) or \
              data.get("data", {}).get(prefix, {}).get("qfqday", [])
        result = []
        for row in raw:
            result.append({
                "date": row[0],
                "open": float(row[1]),
                "close": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "volume": float(row[5]),
            })
        _KLINE_CACHE[code] = result
        return result
    except Exception as e:
        logger.warning("fetch_daily_kline(%s): %s", code, e)
        return []


# ──────────────────────────────────────────────
#  Intraday analysis (market.db minute data)
# ──────────────────────────────────────────────

def get_intraday_metrics(code: str) -> dict:
    """Extract intraday patterns from market.db minute-level data.

    Returns dict with:
      vwap: volume-weighted average price (None if no data)
      range_position: where close sits in day's high-low range (0-100)
      tail_direction: 'up' (尾盘拉升), 'down' (尾盘加速跌), 'flat'
      vol_profile: 'concentrated_open' | 'concentrated_close' | 'spread'
      close_vs_vwap: 'above' | 'below' | 'at'
    """
    if code in _INTRADAY_CACHE:
        return _INTRADAY_CACHE[code]

    result: dict = {
        "vwap": None, "range_position": 50, "tail_direction": "flat",
        "vol_profile": "spread", "close_vs_vwap": "at",
    }

    if not DB_PATH.exists():
        _INTRADAY_CACHE[code] = result
        return result

    try:
        import sqlite3
        db = sqlite3.connect(str(DB_PATH))
        # Get latest trading day's minute data
        rows = db.execute('''
            SELECT quote_time, current_price, volume_shares, high_price, low_price
            FROM quotes WHERE code=? 
            ORDER BY quote_time DESC LIMIT 50
        ''', (code,)).fetchall()
        db.close()

        if len(rows) < 3:
            _INTRADAY_CACHE[code] = result
            return result

        # Get the latest trading day
        latest_date = rows[0][0][:10]
        day_rows = [r for r in rows if r[0][:10] == latest_date]
        day_rows.reverse()  # chronological order

        if len(day_rows) < 3:
            _INTRADAY_CACHE[code] = result
            return result

        prices = [r[1] for r in day_rows]
        volumes = [r[2] for r in day_rows]
        highs = [r[3] for r in day_rows]
        lows = [r[4] for r in day_rows]

        day_high = max(highs)
        day_low = min(lows)
        close_price = prices[-1]

        # ── VWAP ──
        period_vols = []
        for i in range(len(volumes)):
            prev = volumes[i-1] if i > 0 else 0
            period_vols.append(max(0, volumes[i] - prev))

        total_vol = sum(period_vols)
        if total_vol > 0:
            vwap = sum(p * v for p, v in zip(prices, period_vols)) / total_vol
            result["vwap"] = round(vwap, 2)

        # ── Range position ──
        if day_high > day_low:
            result["range_position"] = round((close_price - day_low) / (day_high - day_low) * 100)

        # ── Tail direction (last 3 data points) ──
        if len(prices) >= 3:
            tail_change = prices[-1] - prices[-3]
            if tail_change > 0.05:
                result["tail_direction"] = "up"
            elif tail_change < -0.05:
                result["tail_direction"] = "down"

        # ── Volume concentration ──
        n = len(day_rows)
        first_third_vol = sum(period_vols[:max(1, n//3)])
        last_third_vol = sum(period_vols[-(n//3):])
        if total_vol > 0:
            if first_third_vol / total_vol > 0.5:
                result["vol_profile"] = "concentrated_open"
            elif last_third_vol / total_vol > 0.5:
                result["vol_profile"] = "concentrated_close"

        # ── Close vs VWAP ──
        if result["vwap"] is not None:
            if close_price > result["vwap"] * 1.002:
                result["close_vs_vwap"] = "above"
            elif close_price < result["vwap"] * 0.998:
                result["close_vs_vwap"] = "below"

    except Exception as e:
        logger.warning("get_intraday_metrics(%s): %s", code, e)

    _INTRADAY_CACHE[code] = result
    return result


# ──────────────────────────────────────────────
#  Trend analysis helpers
# ──────────────────────────────────────────────

def is_trend_turning_down(code: str) -> bool:
    """MA5 < MA20 AND last 3 days volume declining → trend is turning bearish."""
    klines = fetch_daily_kline(code, 25)
    if len(klines) < 20:
        return False
    closes = [k["close"] for k in klines]
    volumes = [k["volume"] for k in klines]
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    vol_recent = sum(volumes[-3:]) / 3
    vol_prior = sum(volumes[-8:-3]) / 5 if len(volumes) >= 8 else vol_recent * 1.5
    return ma5 < ma20 and vol_recent < vol_prior * 0.85


def consecutive_decline_days(code: str) -> int:
    """Count consecutive days with lower closes (true downtrend)."""
    klines = fetch_daily_kline(code, 15)
    if len(klines) < 2:
        return 0
    count = 0
    for i in range(len(klines) - 1, 0, -1):
        if klines[i]["close"] < klines[i-1]["close"]:
            count += 1
        else:
            break
    return count


def has_reversal_signal(code: str) -> bool:
    """Volume spike ≥2x average AND single-day gain >+5%."""
    klines = fetch_daily_kline(code, 15)
    if len(klines) < 10:
        return False
    volumes = [k["volume"] for k in klines]
    avg_vol = sum(volumes[-10:-1]) / 9 if len(volumes) >= 10 else sum(volumes[:-1]) / max(len(volumes)-1, 1)
    last = klines[-1]
    last_chg = (last["close"] - last["open"]) / last["open"] * 100
    return last["volume"] > avg_vol * 2.0 and last_chg > 5.0


# ──────────────────────────────────────────────
#  Holdings loading
# ──────────────────────────────────────────────

def load_holdings() -> dict[str, dict]:
    """Load current positions: code -> {quantity, cost_price, current_price, pnl_pct}."""
    holdings: dict[str, dict] = {}
    if not SNAPSHOT_PATH.exists():
        return holdings
    try:
        data = json.loads(SNAPSHOT_PATH.read_text())
        for h in data.get("holdings", []):
            code = h.get("code", "")
            qty = h.get("quantity", 0)
            cost = Decimal(str(h.get("costPrice", 0)))
            price = Decimal(str(h.get("currentPrice", 0)))
            pnl = float((price - cost) / cost * 100) if cost > 0 else 0
            holdings[code] = {
                "quantity": qty,
                "cost_price": float(cost),
                "current_price": float(price),
                "pnl_pct": pnl,
            }
    except Exception as e:
        logger.warning("load_holdings: %s", e)
    return holdings


# ──────────────────────────────────────────────
#  Message parsing
# ──────────────────────────────────────────────

def extract_code_and_action(msg: str) -> tuple[str | None, str | None]:
    """Extract 6-digit stock code and action (buy/sell) from message text.
    If both buy and sell appear, returns None to signal ambiguity."""
    code_match = re.search(r'\b(\d{6})\b', msg)
    code = code_match.group(1) if code_match else None

    has_buy = bool(re.search(r'买入|buy', msg, re.IGNORECASE)) and not bool(
        re.search(r'不建议买入|不买入|暂不买入|不推荐买入|buy\s+signal\s*[：:]?\s*no', msg, re.IGNORECASE))
    has_sell = bool(re.search(r'卖出|清仓|减仓|reduce|avoid|sell', msg, re.IGNORECASE)) and not bool(
        re.search(r'不建议卖出|不卖出|暂不卖出', msg))

    # Ambiguous: both buy and sell detected in same message — don't guess
    if has_buy and has_sell:
        return code, None
    if has_buy:
        return code, "buy"
    if has_sell:
        return code, "sell"
    return code, None


def is_briefing(msg: str) -> bool:
    """Check if message is a briefing/review (always pass through)."""
    return bool(re.search(r'数据复盘|收盘复盘|盘前简报|今日速判|复盘结论|复盘文档|多Agent辩论', msg))


# ──────────────────────────────────────────────
#  Core validation
# ──────────────────────────────────────────────

def validate_entry(item: dict, holdings: dict[str, dict]) -> tuple[bool, str]:
    """Returns (should_deliver, reason_if_skipped)."""
    msg = item.get("message", "")
    code, action = extract_code_and_action(msg)

    # Pass-through: briefing messages always delivered
    if is_briefing(msg):
        return True, ""

    if not code or not action:
        return True, ""  # No actionable signal → pass through

    holding = holdings.get(code)

    # New stock, not in holdings → allow buy, but block sell (can't sell what you don't own)
    if not holding:
        if action == "sell":
            return False, "未持仓，拦截卖出"
        return True, ""

    pnl = holding["pnl_pct"]
    qty = holding["quantity"]
    intraday = get_intraday_metrics(code)

    # ── RULE 1: Selling winner (>+3%) ──
    if action == "sell" and pnl > 3.0:
        if is_trend_turning_down(code):
            return True, f"盈利{pnl:+.1f}%但MA5<MA20+缩量，趋势转弱，放行卖出"
        # 即使均线未死叉，收盘低于VWAP+尾盘跌 → 动能衰竭
        if intraday["close_vs_vwap"] == "below" and intraday["tail_direction"] == "down":
            return True, f"盈利{pnl:+.1f}%但收盘<VWAP+尾盘跌，动能衰竭，放行卖出"
        return False, f"盈利{pnl:+.1f}%且趋势健康，拦截卖出"

    # ── RULE 2: Selling small loser (-5% to +3%) ──
    if action == "sell" and pnl > -5.0:
        decline_days = consecutive_decline_days(code)

        # 缩量阴跌：早盘放量(出货)+尾盘跌+收盘低于VWAP → 强卖信号
        if (intraday["vol_profile"] == "concentrated_open" and
                intraday["tail_direction"] == "down" and
                intraday["close_vs_vwap"] == "below"):
            return True, f"缩量阴跌(早盘放量出货+尾盘加速跌)，放行止损 pnl={pnl:.1f}%"

        # 弱势收盘：收盘在日低1/3区间+尾盘跌+不高于VWAP → 卖
        if (intraday["range_position"] < 35 and
                intraday["tail_direction"] == "down" and
                intraday["close_vs_vwap"] != "above"):
            return True, f"弱势收盘(低位{intraday['range_position']}%+尾盘跌)，放行止损 pnl={pnl:.1f}%"

        # 放量探底：尾盘放量+拉升+VWAP上方 → 疑似反转，拦截
        if (intraday["vol_profile"] == "concentrated_close" and
                intraday["tail_direction"] == "up" and
                intraday["close_vs_vwap"] == "above"):
            return False, f"放量探底(尾盘放量拉升+VWAP上方)，疑似反转，拦截卖出 pnl={pnl:.1f}%"

        if decline_days >= 5:
            return True, f"连续阴跌{decline_days}天，放行止损 pnl={pnl:.1f}%"
        if decline_days >= 3 and pnl < -2.0:
            return True, f"连跌{decline_days}天+浮亏{pnl:.1f}%，放行减仓"
        return False, f"小亏{pnl:.1f}%仅连跌{decline_days}天，拦截卖出"

    # ── RULE 3: Buying deep loser (<-20%) ──
    if action == "buy" and pnl < -20.0:
        if has_reversal_signal(code):
            return True, f"深套{pnl:.1f}%但出现放量反弹信号，放行试探买"
        return False, f"深套{pnl:.1f}%，无反转信号，拦截买入"

    # ── RULE 4: Zero-hold sell ──
    if action == "sell" and qty <= 0:
        return False, "零持仓，拦截卖出"

    return True, ""


# ──────────────────────────────────────────────
#  State management (track which items to consume)
# ──────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"validated_hashes": [], "validated_at": None}


def save_state(validated_hashes: list[str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({
        "validated_hashes": validated_hashes,
        "validated_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False))


# ── Block cooldown ──

def _load_block_cooldown() -> dict[str, float]:
    """Load {(code,action): last_blocked_ts} from disk."""
    if BLOCK_COOLDOWN_PATH.exists():
        try:
            return json.loads(BLOCK_COOLDOWN_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_block_cooldown(data: dict[str, float]) -> None:
    BLOCK_COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    BLOCK_COOLDOWN_PATH.write_text(json.dumps(data, ensure_ascii=False))


def _is_in_cooldown(code: str, action: str, cooldown: dict[str, float]) -> bool:
    """Check if this (code, action) was blocked within cooldown window."""
    key = f"{code}:{action}"
    last = cooldown.get(key, 0)
    return (time.time() - last) < BLOCK_COOLDOWN_SECONDS


# ──────────────────────────────────────────────
#  Trigger alert checking (independent of outbox)
# ──────────────────────────────────────────────

def _fetch_trigger_prices(codes: list[str]) -> dict[str, float]:
    """Fetch current prices from Tencent qt API for trigger checking."""
    if not codes:
        return {}
    symbols = ",".join(f"sh{c}" if c.startswith("6") else f"sz{c}" for c in codes)
    url = f"https://qt.gtimg.cn/q={symbols}"
    prices: dict[str, float] = {}
    try:
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        r.encoding = "gbk"
        for line in r.text.strip().split("\n"):
            if '="' not in line:
                continue
            fields = line.split("~")
            if len(fields) < 5:
                continue
            code = fields[2].strip()
            try:
                prices[code] = float(fields[3])
            except (ValueError, IndexError):
                continue
    except Exception as e:
        logger.warning("_fetch_trigger_prices: %s", e)
    return prices


def _load_trigger_cooldown() -> dict[str, dict]:
    """Load trigger alert cooldown state.
    Format: {key: {"ts": float, "count": int}} or legacy {key: float}.
    """
    if TRIGGER_COOLDOWN_PATH.exists():
        try:
            raw = json.loads(TRIGGER_COOLDOWN_PATH.read_text())
            # Normalize: legacy format {key: float} -> {key: {"ts": float, "count": 1}}
            normalized = {}
            for k, v in raw.items():
                if isinstance(v, (int, float)):
                    normalized[k] = {"ts": float(v), "count": 1}
                elif isinstance(v, dict):
                    normalized[k] = v
            return normalized
        except Exception:
            pass
    return {}


def _save_trigger_cooldown(data: dict[str, dict]) -> None:
    TRIGGER_COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRIGGER_COOLDOWN_PATH.write_text(json.dumps(data, ensure_ascii=False))


def _format_cash_summary() -> str:
    """Return a one-line cash reserve summary from the portfolio snapshot, or empty."""
    if not SNAPSHOT_PATH.exists():
        return ""
    try:
        snap = json.loads(SNAPSHOT_PATH.read_text())
        total = snap.get("totalAssets", 0)
        cash = snap.get("cash", 0)
        if not total or not cash:
            return ""
        ratio = cash / total * 100
        return f"💰 现金储备：{cash:,.2f} / 总资产{total:,.2f}（{ratio:.0f}%）"
    except Exception:
        return ""


def _get_fund_flow_hint(code: str) -> str:
    """Get a one-line fund flow hint for a stock. Cached 10 min."""
    global _FUND_FLOW_CACHE
    now = time.time()
    if code in _FUND_FLOW_CACHE:
        ts, hint = _FUND_FLOW_CACHE[code]
        if now - ts < 600:  # 10 min cache
            return hint
    try:
        from stock_advisor.chrome_scraper import get_stock_fund_flow
        ff = get_stock_fund_flow(code)
        if ff and ff.get("main_net_yi") is not None:
            mn = ff["main_net_yi"]
            d = "🟢" if mn > 0 else ("🔴" if mn < 0 else "⚪")
            hint = f"💧 主力{d}{abs(mn):.2f}亿"
            _FUND_FLOW_CACHE[code] = (now, hint)
            return hint
    except Exception:
        pass
    return ""


def _load_holdings_map() -> dict[str, dict]:
    """Load portfolio holdings keyed by stock code.
    Returns {code: {name, quantity, costPrice, currentPrice}} or empty dict."""
    if not SNAPSHOT_PATH.exists():
        return {}
    try:
        snap = json.loads(SNAPSHOT_PATH.read_text())
        holdings = snap.get("holdings", []) or snap.get("positions", {}).values()
    except Exception:
        return {}
    result = {}
    for h in holdings:
        code = str(h.get("code", "")).zfill(6)
        if code:
            result[code] = {
                "name": h.get("name", ""),
                "quantity": int(h.get("quantity", 0)),
                "costPrice": float(h.get("costPrice", 0)),
                "currentPrice": float(h.get("currentPrice", 0)),
            }
    return result


def _make_trigger_key(trigger: dict) -> str:
    """Build a stable identity key for a trigger instance.

    Name-only keys caused newly regenerated debate/exit triggers to inherit old
    cooldown counts and get auto-disabled on the next session. Include source and
    creation timestamp when available so a fresh trigger starts fresh.
    """
    code = trigger.get("code", "")
    name = trigger.get("name", "")
    source = trigger.get("_source", "")
    created = trigger.get("_created", "")
    parts = [str(code), str(name)]
    if source:
        parts.append(str(source))
    if created:
        parts.append(str(created))
    return ":".join(parts)



def _bump_trigger_cooldown(cooldown: dict, key: str, now: float) -> int:
    """Increment the fired count for a trigger key. Returns the new count."""
    entry = cooldown.get(key, {})
    if isinstance(entry, dict):
        count = entry.get("count", 0) + 1
    else:
        count = 2  # legacy single-fire → now it's the 2nd
    cooldown[key] = {"ts": now, "count": count}
    return count


def _auto_disable_triggers(keys: set[str]) -> None:
    """Remove triggers from trading_plan.json that have exceeded max firings.

    If a debate_sync sell trigger is auto-disabled, also remove same-code exit_plan_sync
    sell triggers so a softer partial-sell plan does not resurface and contradict the
    harder debate/risk instruction the user just received.
    """
    plan_path = REPO / "data" / "trading_plan.json"
    if not plan_path.exists():
        return
    try:
        plan = json.loads(plan_path.read_text())
    except Exception:
        return

    triggers = plan.get("triggers", [])
    direct_removed = []
    debate_removed_codes: set[str] = set()

    for t in triggers:
        key = _make_trigger_key(t)
        legacy_key = f"{t['code']}:{t['name']}"
        has_new_key = bool(t.get("_source") or t.get("_created"))
        matched = key in keys or (not has_new_key and legacy_key in keys)
        if matched:
            direct_removed.append(key)
            if t.get("_source") == "debate_sync" and str(t.get("action", "")) == "sell":
                debate_removed_codes.add(str(t.get("code", "")))

    removed = []
    kept = []
    for t in triggers:
        key = _make_trigger_key(t)
        legacy_key = f"{t['code']}:{t['name']}"
        code = str(t.get("code", ""))
        source = t.get("_source")
        has_new_key = bool(t.get("_source") or t.get("_created"))
        is_direct = key in keys or (not has_new_key and legacy_key in keys)
        is_shadowed_exit = (
            code in debate_removed_codes
            and source == "exit_plan_sync"
            and str(t.get("action", "")) == "sell"
        )
        if is_direct or is_shadowed_exit:
            removed.append(key)
        else:
            kept.append(t)

    if removed:
        plan["triggers"] = kept
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=4) + "\n")
        log_path = REPO / "data" / "bridge_skipped.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().isoformat()}] Auto-disabled triggers: {removed}\n")


def _is_trigger_delivery_window() -> bool:
    """Only deliver actionable trigger alerts during the trading-day order window."""
    now = datetime.now(MARKET_TZ)
    if now.weekday() >= 5:
        return False
    current = now.time()
    return current >= datetime.strptime("09:00", "%H:%M").time() and current < datetime.strptime("15:05", "%H:%M").time()


def _check_triggers() -> list[str]:
    """Check trading_plan.json triggers against current prices.
    Returns alert messages for breached triggers. Uses cooldown to avoid spam.
    This runs independently of outbox — price triggers don't need scoring signals."""
    if not _is_trigger_delivery_window():
        return []

    plan_path = REPO / "data" / "trading_plan.json"
    if not plan_path.exists():
        return []

    try:
        plan = json.loads(plan_path.read_text())
    except Exception:
        return []

    triggers = plan.get("triggers", [])
    if not triggers:
        return []

    codes = list(set(t["code"] for t in triggers))
    prices = _fetch_trigger_prices(codes)
    if not prices:
        return []

    holdings_map = _load_holdings_map()

    def _holdings_line(code: str) -> str:
        """Return a holdings summary line for the given stock code, or empty string."""
        h = holdings_map.get(code)
        if not h or h["quantity"] <= 0:
            return ""
        return (
            f"📊 持仓：{h['name']} {h['quantity']}股 "
            f"成本{h['costPrice']:.2f} 现价{h['currentPrice']:.2f}"
        )

    cooldown = _load_trigger_cooldown()
    now = time.time()
    alert_entries: list[dict] = []  # {\"text\": str, \"keys\": list[str]}
    updated = False

    # ── Phase 1: collect raw fired triggers per stock ──
    fired_by_code: dict[str, list[dict]] = {}  # code -> [{trigger, current, status, gap}]
    for t in triggers:
        # ── Phase 2: only fire triggers in armed state ──
        if t.get("state", "armed") != "armed":
            continue

        code = t["code"]
        name = t.get("name", code)
        action = t.get("action", "sell")
        price_min = float(t.get("priceMin", 0))
        price_max = float(t.get("priceMax", 0))
        current = prices.get(code)

        # Validate trigger price range
        if current is None:
            continue
        if price_min <= 0 or price_max <= 0:
            continue
        if price_max < price_min:
            # Malformed trigger — swap or skip
            price_min, price_max = price_max, price_min

        key = _make_trigger_key(t)
        legacy_key = f"{code}:{name}"
        has_new_key = bool(t.get("_source") or t.get("_created"))
        entry = cooldown.get(key)
        if entry is None and not has_new_key:
            # Only fall back to legacy key if the trigger itself is legacy (no _source/_created).
            # New-format triggers must not inherit stale legacy cooldown counts.
            entry = cooldown.get(legacy_key, {})
        if entry is None:
            entry = {}
        last = entry.get("ts", 0) if isinstance(entry, dict) else 0
        if (now - last) < TRIGGER_ALERT_COOLDOWN:
            continue

        in_zone = price_min <= current <= price_max
        below = current < price_min
        above = current > price_max

        # Compute gap for status display
        if in_zone:
            gap = 0.0
        elif below:
            gap = (current - price_min) / price_min * 100
        else:
            gap = (current - price_max) / price_max * 100

        if action == "sell":
            # Sell triggers only fire when price is in or above the zone.
            # Below the zone means the bounce hasn't happened yet — not actionable.
            if current < price_min:
                continue
            if in_zone:
                status = f"🎯 进入卖出区间 {price_min:.2f}-{price_max:.2f}，建议挂单"
            else:
                # Price above sell zone = can sell at a better price than planned.
                # This is NOT "missed" — it's a windfall. Sell now.
                status = f"🟢 已突破卖点 {gap:+.1f}%（原区间{price_min:.2f}-{price_max:.2f}），现价更优，立即卖出"
        else:  # buy
            if in_zone:
                status = f"🎯 进入买入区间 {price_min:.2f}-{price_max:.2f}，可考虑建仓"
            elif below:
                status = f"🟢 已跌破买入区 {gap:+.1f}%（区间{price_min:.2f}-{price_max:.2f}），可更低吸筹"
            else:
                status = f"🔴 已涨过买入区 {gap:+.1f}%（区间{price_min:.2f}-{price_max:.2f}），错过买点"

        fired_by_code.setdefault(code, []).append({
            "trigger": t, "name": name, "action": action, "current": current,
            "price_min": price_min, "price_max": price_max,
            "status": status, "gap": gap, "in_zone": in_zone,
            "key": key,
        })

    # ── Semantic sanity: fix misleading status for sell triggers above zone ──
    # When current >> price_max on a sell trigger, the zone is stale (generated
    # from yesterday's snapshot).  Rewrite status so it doesn't say "missed".
    STALE_ZONE_THRESHOLD = 5.0  # zone off by >5% → stale
    for code, items in fired_by_code.items():
        for item in items:
            if item["action"] != "sell" or item["in_zone"]:
                continue
            gap = item.get("gap", 0)
            if gap > STALE_ZONE_THRESHOLD:
                item["status"] = (
                    f"🟢 现价 {item['current']:.2f} 远超原卖点 "
                    f"({item['price_min']:.2f}-{item['price_max']:.2f}) +{gap:.1f}%，"
                    f"原触发区间已过期，按移动止盈峰值回撤执行"
                )

    # ── Phase 2: consolidate per stock, resolve direction conflicts ──
    is_trading = is_a_share_trading_time()
    for code, fired in fired_by_code.items():
        # 非交易时段（收盘后/盘前/周末）：只推 in_zone（🎯）触发单
        # "错过卖点" / "已涨过" / "已跌破" 全是事后噪音，用户无法操作
        if not is_trading:
            fired = [f for f in fired if f["in_zone"]]
            if not fired:
                continue
        buys = [f for f in fired if f["action"] == "buy"]
        sells = [f for f in fired if f["action"] == "sell"]
        debate_sells = [f for f in sells if f["trigger"].get("_source") == "debate_sync"]
        if debate_sells:
            sells = debate_sells
        elif sells:
            exit_plan_sells = [f for f in sells if f["trigger"].get("_source") == "exit_plan_sync"]
            if exit_plan_sells:
                sells = exit_plan_sells
        fired = buys + sells if sells else buys
        if not fired:
            continue
        holdings_info = _holdings_line(code)
        ts = datetime.now().strftime("%m-%d %H:%M:%S")

        if buys and sells:
            # ⚠️ Direction conflict → auto-resolve: pick single most relevant trigger
            # "Most relevant" = whose zone boundary is closest to current price.
            # Price dropped below all zones? Buy trigger wins (closest). Price surged
            # above all zones? Sell trigger wins. No manual conflict alerts.
            all_fired = buys + sells
            best = min(all_fired, key=lambda f: min(
                abs(f["current"] - f["price_min"]),
                abs(f["current"] - f["price_max"]),
            ))
            # Suppressed trigger names for transparency
            others = [f for f in all_fired if f != best]
            other_names = [f["name"].split("-", 1)[-1] for f in others]
            suppressed = f"\n📎 已抑制矛盾触发单：{', '.join(other_names)}" if others else ""

            f = best
            t = f["trigger"]
            direction = "卖出" if f["action"] == "sell" else "买入"
            qty = t.get("quantity", "?")
            note = t.get("note", "")
            if len(note) > MAX_NOTE_LENGTH:
                note = note[:MAX_NOTE_LENGTH] + "…"
            hl = f"\n{holdings_info}" if holdings_info else ""
            ff_hint = _get_fund_flow_hint(code) if code else ""

            alert = (
                f"🔔 **触发单警报：{f['name']}** `{ts}`\n"
                f"方向：{direction} {qty}股\n"
                f"现价：{f['current']:.2f}\n"
                f"状态：{f['status']}{suppressed}{hl}\n"
                f"备注：{note}{chr(10)+ff_hint if ff_hint else ''}"
            )
            alert_entries.append({"text": alert, "keys": [item["key"] for item in all_fired]})
            updated = True

        elif sells:
            # Multiple sell triggers — keep only the most urgent (closest above current)
            most_urgent = min(sells, key=lambda f: abs(f["current"] - f["price_min"]))
            f = most_urgent
            t = f["trigger"]
            direction = "卖出"
            qty = t.get("quantity", "?")
            note = t.get("note", "")
            if len(note) > MAX_NOTE_LENGTH:
                note = note[:MAX_NOTE_LENGTH] + "…"

            # If other sell triggers also fired, add a summary line
            extra = ""
            if len(sells) > 1:
                other_names = [s["name"].split("-", 1)[-1] for s in sells if s != most_urgent]
                extra = f"\n⚠️ 另{len(sells)-1}个卖出触发单同时激活：{', '.join(other_names)}"
            hl = f"\n{holdings_info}" if holdings_info else ""
            ff_hint = _get_fund_flow_hint(code) if code else ""

            alert = (
                f"🔔 **触发单警报：{f['name']}** `{ts}`\n"
                f"方向：{direction} {qty}股\n"
                f"现价：{f['current']:.2f}\n"
                f"状态：{f['status']}{extra}{hl}\n"
                f"备注：{note}{chr(10)+ff_hint if ff_hint else ''}"
            )
            alert_entries.append({"text": alert, "keys": [item["key"] for item in sells]})
            updated = True

        elif buys:
            # Multiple buy triggers — keep only the most relevant
            most_relevant = min(buys, key=lambda f: abs(f["current"] - f["price_min"]))
            f = most_relevant
            t = f["trigger"]
            direction = "买入"
            qty = t.get("quantity", "?")
            note = t.get("note", "")
            if len(note) > MAX_NOTE_LENGTH:
                note = note[:MAX_NOTE_LENGTH] + "…"
            hl = f"\n{holdings_info}" if holdings_info else ""

            alert = (
                f"🔔 **触发单警报：{f['name']}** `{ts}`\n"
                f"方向：{direction} {qty}股\n"
                f"现价：{f['current']:.2f}\n"
                f"状态：{f['status']}{hl}\n"
                f"备注：{note}"
            )
            alert_entries.append({"text": alert, "keys": [f["key"]]})
            updated = True

    # ── Cap alerts per tick (BEFORE writing cooldown, so truncated alerts can fire next round) ──
    overflow_entries: list[dict] = []
    if len(alert_entries) > MAX_ALERTS_PER_RUN:
        overflow_entries = alert_entries[MAX_ALERTS_PER_RUN:]
        alert_entries = alert_entries[:MAX_ALERTS_PER_RUN]

    # ── Write cooldown ONLY for alerts that actually survived truncation ──
    for entry in alert_entries:
        for key in entry["keys"]:
            _bump_trigger_cooldown(cooldown, key, now)
        updated = True

    # ── Auto-disable triggers that have fired too many times ──
    triggers_to_remove: set[str] = set()
    for key, entry in cooldown.items():
        count = entry.get("count", 0) if isinstance(entry, dict) else 0
        if count >= MAX_TRIGGER_FIRINGS:
            triggers_to_remove.add(key)

    if triggers_to_remove:
        _auto_disable_triggers(triggers_to_remove)

    if updated:
        _save_trigger_cooldown(cooldown)

    # ── Build final alert strings ──
    alerts: list[str] = [e["text"] for e in alert_entries]

    # ── Append cash reserve summary if any alerts ──
    if alerts:
        cash_line = _format_cash_summary()
        if cash_line:
            alerts.append(cash_line)

    # ── Append overflow hint if any were truncated ──
    if overflow_entries:
        alerts.append(f"📎 另有{len(overflow_entries)}条触发单因长度限制未推送，下轮继续")

    return alerts


# ──────────────────────────────────────────────
#  Anomaly detection (independent — no triggers needed)
# ──────────────────────────────────────────────

def _load_anomaly_cooldown() -> dict[str, float]:
    """Load anomaly alert cooldown state. Format: {type:code: ts}."""
    if ANOMALY_COOLDOWN_PATH.exists():
        try:
            return json.loads(ANOMALY_COOLDOWN_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_anomaly_cooldown(data: dict[str, float]) -> None:
    ANOMALY_COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    ANOMALY_COOLDOWN_PATH.write_text(json.dumps(data, ensure_ascii=False))


def _fetch_holding_prices_with_chg() -> dict[str, dict]:
    """Fetch current price + chg_pct + name from Tencent for all holdings.
    Returns {code: {price, chg_pct, name}}."""
    holdings = load_holdings()
    if not holdings:
        return {}

    codes = list(holdings.keys())
    symbols = ",".join(f"sh{c}" if c.startswith("6") else f"sz{c}" for c in codes)
    url = f"https://qt.gtimg.cn/q={symbols}"
    result: dict[str, dict] = {}

    try:
        r = requests.get(url, timeout=8)
        r.encoding = "gbk"
        for line in r.text.strip().split("\n"):
            if '="' not in line:
                continue
            fields = line.split("~")
            if len(fields) < 33:
                continue
            code = fields[2].strip()
            try:
                price = float(fields[3])
                name = fields[1].strip()
                chg_pct = float(fields[32]) if fields[32] else 0.0
                result[code] = {"price": price, "chg_pct": chg_pct, "name": name}
            except (ValueError, IndexError):
                continue
    except Exception:
        pass
    return result


def _check_anomalies() -> list[str]:
    """Detect anomalous conditions on holdings: fund outflow, sudden drop.
    Cooldown-limited: max 1 alert per (type, code) per 10 min."""
    if not _is_trigger_delivery_window():
        return []

    holdings = load_holdings()
    if not holdings:
        return []

    # Fetch prices with change percent
    prices = _fetch_holding_prices_with_chg()
    if not prices:
        return []

    cooldown = _load_anomaly_cooldown()
    now = time.time()
    alerts: list[str] = []
    updated = False

    for code, h in holdings.items():
        pq = prices.get(code)
        if not pq or h.get("quantity", 0) <= 0:
            continue

        pct = pq["chg_pct"]
        name = pq["name"]

        # ── 1. Sudden drop (>2%) ──
        drop_key = f"DROP:{code}"
        if pct <= SUDDEN_DROP_THRESHOLD_PCT:
            last = cooldown.get(drop_key, 0)
            if (now - last) >= ANOMALY_COOLDOWN_SECONDS:
                alerts.append(
                    f"⚠️ **盘中异动：{name}** `{datetime.now(MARKET_TZ).strftime('%H:%M')}`\n"
                    f"日内跌幅 {pct:+.1f}%，现价 {pq['price']:.2f}\n"
                    f"📊 持仓 {h['quantity']}股 成本{h['cost_price']:.2f}"
                )
                cooldown[drop_key] = now
                updated = True

        # ── 2. 主力资金大单净流出 ──
        flow_key = f"FLOW:{code}"
        last = cooldown.get(flow_key, 0)
        if (now - last) >= ANOMALY_COOLDOWN_SECONDS:
            try:
                from stock_advisor.chrome_scraper import get_stock_fund_flow_realtime
                ff = get_stock_fund_flow_realtime(code)
                if ff and ff.get("cumulative_yi") is not None:
                    cumulative = float(ff["cumulative_yi"])
                    if cumulative <= FUND_OUTFLOW_THRESHOLD_YI:
                        alerts.append(
                            f"⚠️ **资金异动：{name}** `{datetime.now(MARKET_TZ).strftime('%H:%M')}`\n"
                            f"主力净流出 {cumulative:.2f}亿，现价 {pq['price']:.2f}\n"
                            f"📊 持仓 {h['quantity']}股 成本{h['cost_price']:.2f}"
                        )
                        cooldown[flow_key] = now
                        updated = True
            except Exception:
                pass

        # ── 3. 板块大涨个股不跟（sector surge but stock lags）──
        lag_key = f"LAG:{code}"
        last = cooldown.get(lag_key, 0)
        if (now - last) >= ANOMALY_COOLDOWN_SECONDS:
            try:
                from stock_advisor.market_breadth import get_sectors, STOCK_SECTORS
                stock_secs = STOCK_SECTORS.get(code, [])
                sectors_data = get_sectors()
                sec_map = {s["code"]: s for s in sectors_data}
                for sc in stock_secs:
                    # STOCK_SECTORS uses "pt01801102" but get_sectors returns "01801102"
                    clean_sc = sc[2:] if sc.startswith("pt") else sc
                    sec = sec_map.get(clean_sc) or sec_map.get(sc)
                    if not sec:
                        continue
                    sec_chg = float(sec["chg_pct"])
                    if sec_chg >= SECTOR_SURGE_THRESHOLD and (sec_chg - pct) >= SECTOR_LAG_GAP:
                        alerts.append(
                            f"🔴 **板块背离：{name}** `{datetime.now(MARKET_TZ).strftime('%H:%M')}`\n"
                            f"{sec['name']}板块 {sec_chg:+.1f}%，{name}仅 {pct:+.1f}%（跑输 {sec_chg - pct:.1f}个百分点）\n"
                            f"📊 板块大涨个股不跟，极度弱势⚠️"
                        )
                        cooldown[lag_key] = now
                        updated = True
                        break  # One alert per stock
            except Exception:
                pass

        if len(alerts) >= MAX_ANOMALIES_PER_RUN:
            break

    if updated:
        _save_anomaly_cooldown(cooldown)

    return alerts


# ──────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────

def run_validate() -> int:
    """Validate outbox entries + check trigger alerts.
    Output passing messages to stdout. Returns exit code."""
    # ── Step 0: Check trading plan triggers (independent of outbox) ──
    trigger_alerts = _check_triggers()
    if trigger_alerts:
        for alert in trigger_alerts:
            print(alert)
            print("---")

    # ── Step 0b: Check anomalies on holdings (fund outflow, sudden drop) ──
    anomaly_alerts = _check_anomalies()
    if anomaly_alerts:
        for alert in anomaly_alerts:
            print(alert)
            print("---")

    if not OUTBOX_PATH.exists():
        return 0

    holdings = load_holdings()
    block_cooldown = _load_block_cooldown()
    outbox_content = OUTBOX_PATH.read_text(encoding="utf-8")
    lines = outbox_content.splitlines()

    parsed: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    pending = [item for item in parsed if not item.get("sent")]
    if not pending:
        return 0

    valid_items: list[dict] = []
    skipped_reasons: list[str] = []
    skipped_count = 0
    cooldown_skip_count = 0

    for item in pending[:10]:  # Process max 10 per tick
        code, action = extract_code_and_action(item.get("message", ""))
        should_deliver, reason = validate_entry(item, holdings)
        if should_deliver:
            valid_items.append(item)
        else:
            # Cooldown: silently skip if same (code, action) was blocked <5 min ago
            if code and action and _is_in_cooldown(code, action, block_cooldown):
                cooldown_skip_count += 1
                continue
            if code and action:
                block_cooldown[f"{code}:{action}"] = time.time()
            skipped_count += 1
            pnl = holdings.get(code, {}).get("pnl_pct", 0) if code else 0
            skipped_reasons.append(
                f"SKIP {code or '???'}: {item.get('title','?')[:40]} ({reason})"
            )

    # Persist cooldown
    _save_block_cooldown(block_cooldown)

    # Log skipped
    if skipped_reasons:
        SKIPPED_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(SKIPPED_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().isoformat()}] Validated {len(pending[:10])} pending, "
                    f"passed {len(valid_items)}, skipped {skipped_count}"
                    + (f", cooldown {cooldown_skip_count}" if cooldown_skip_count else "")
                    + ":\n")
            for s in skipped_reasons:
                f.write(f"  {s}\n")

    # Save state so consume knows exactly which items to mark
    validated_hashes: list[str] = [_item_hash(item) for item in valid_items]
    save_state(validated_hashes)

    # Output validated messages
    if not valid_items:
        return 0

    for item in valid_items:
        print(item["message"])
        print("---")

    return 0


def _mark_sent(validated_hashes: set[str] | None = None, limit: int = 10) -> int:
    """Mark validated outbox entries as sent.

    If validated_hashes is provided, only items whose message hash matches
    will be marked. Otherwise falls back to marking up to `limit` unsent items
    (legacy behavior for run_deliver which validates in-process).
    Returns count marked."""
    if not OUTBOX_PATH.exists():
        return 0

    from .platform_compat import lock_file, unlock_file
    fd = os.open(str(OUTBOX_PATH), os.O_RDWR)
    lock_file(fd)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        data = os.read(fd, 10 * 1024 * 1024)
        rows = data.decode("utf-8").splitlines()

        marked = 0
        updated: list[dict] = []
        for row in rows:
            if not row.strip():
                continue
            item = json.loads(row)
            if not item.get("sent"):
                item_hash = _item_hash(item)
                if validated_hashes is not None:
                    if item_hash in validated_hashes:
                        item["sent"] = True
                        marked += 1
                elif marked < limit:
                    item["sent"] = True
                    marked += 1
            updated.append(item)

        if marked > 0:
            content = "\n".join(json.dumps(i, ensure_ascii=False) for i in updated)
            if updated:
                content += "\n"
            os.lseek(fd, 0, os.SEEK_SET)
            os.truncate(fd, 0)
            os.write(fd, content.encode("utf-8"))
    finally:
        unlock_file(fd)
        os.close(fd)

    return marked


def run_consume() -> int:
    """Mark only the previously-validated batch as sent."""
    state = load_state()
    hashes = set(state.get("validated_hashes", []))
    count = _mark_sent(validated_hashes=hashes) if hashes else 0
    if count:
        print(f"Consumed {count} outbox entries")
    if STATE_PATH.exists():
        STATE_PATH.unlink()
    return 0


# ──────────────────────────────────────────────
#  App DM delivery
# ──────────────────────────────────────────────

def _get_tenant_token(app_id: str, app_secret: str) -> str | None:
    """Get Feishu tenant_access_token."""
    try:
        r = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code", -1) != 0:
            logger.warning("_get_tenant_token: Feishu API error code=%s msg=%s",
                           data.get("code"), data.get("msg", ""))
            return None
        return data.get("tenant_access_token")
    except Exception as e:
        logger.warning("_get_tenant_token: %s", e)
        return None


def _send_app_dm(token: str, receive_open_id: str, text: str) -> bool:
    """Send a direct message via Feishu API. Returns True on success."""
    # Feishu text messages limited to ~20KB, chunk at 15000 safely
    MAX_CHUNK = 15000
    for i in range(0, len(text), MAX_CHUNK):
        chunk = text[i:i + MAX_CHUNK]
        payload = {
            "receive_id": receive_open_id,
            "msg_type": "text",
            "content": json.dumps({"text": chunk}, ensure_ascii=False),
        }
        try:
            r = requests.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": "open_id"},
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("code", -1) != 0:
                logger.warning("_send_app_dm: Feishu API error code=%s msg=%s",
                               data.get("code"), data.get("msg", ""))
                return False
        except Exception as e:
            logger.warning("_send_app_dm: %s", e)
            return False
    return True


def _load_app_config() -> dict | None:
    """Load Feishu delivery config from config.yaml via stock_advisor.config."""
    config_path = REPO / "config.yaml"
    if not config_path.exists():
        return None
    try:
        cfg = load_config(config_path)
        feishu = cfg.monitor.notification.feishu
        bot = cfg.feishu_bot
        return {
            "delivery_mode": feishu.delivery_mode,
            "app_id": bot.app_id,
            "app_secret": bot.app_secret,
            "receive_open_id": feishu.receive_open_id,
            "webhook_url": feishu.webhook_url,
        }
    except Exception as e:
        logger.warning("_load_app_config: %s", e)
    return None


def run_deliver() -> int:
    """Validate (with triggers + anomalies) + deliver + consume in one shot.
    Uses the same validation pipeline as run_validate() so behavior is identical
    regardless of which mode cron invokes."""
    # ── Step 0: Trigger alerts (same as validate) ──
    trigger_alerts = _check_triggers()
    anomaly_alerts = _check_anomalies()

    # ── Step 1: Outbox validation (same logic as validate) ──
    valid_items: list[dict] = []
    validated_hashes: list[str] = []

    if OUTBOX_PATH.exists():
        holdings = load_holdings()
        block_cooldown = _load_block_cooldown()
        outbox_content = OUTBOX_PATH.read_text(encoding="utf-8")
        lines = outbox_content.splitlines()

        parsed: list[dict] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        pending = [item for item in parsed if not item.get("sent")]
        if pending:
            skipped_reasons: list[str] = []
            skipped_count = 0
            cooldown_skip_count = 0

            for item in pending[:10]:
                code, action = extract_code_and_action(item.get("message", ""))
                should_deliver, reason = validate_entry(item, holdings)
                if should_deliver:
                    valid_items.append(item)
                else:
                    if code and action and _is_in_cooldown(code, action, block_cooldown):
                        cooldown_skip_count += 1
                        continue
                    if code and action:
                        block_cooldown[f"{code}:{action}"] = time.time()
                    skipped_count += 1
                    skipped_reasons.append(
                        f"SKIP {code or '???'}: {item.get('title','?')[:40]} ({reason})"
                    )

            _save_block_cooldown(block_cooldown)

            if skipped_reasons:
                SKIPPED_LOG.parent.mkdir(parents=True, exist_ok=True)
                with open(SKIPPED_LOG, "a", encoding="utf-8") as f:
                    f.write(f"\n[{datetime.now().isoformat()}] Validated {min(10, len(pending))} pending, "
                            f"passed {len(valid_items)}, skipped {skipped_count}"
                            + (f", cooldown {cooldown_skip_count}" if cooldown_skip_count else "")
                            + ":\n")
                    for s in skipped_reasons:
                        f.write(f"  {s}\n")

        validated_hashes = [_item_hash(item) for item in valid_items]

    # ── If nothing to deliver (no trigger, no anomaly, no outbox), exit clean ──
    if not (trigger_alerts or anomaly_alerts or valid_items):
        return 0

    # ── Step 2: Get delivery config ──
    cfg = _load_app_config()
    if not cfg:
        print("ERROR: Cannot load Feishu config from config.yaml", file=__import__('sys').stderr)
        return 1

    delivery_mode = cfg.get("delivery_mode", "webhook")

    # ── Step 3: Deliver trigger + anomaly + validated outbox messages ──
    all_delivered = True
    all_items: list[dict] = []

    # Collect trigger alerts as items
    for alert in trigger_alerts:
        all_items.append({"message": alert, "source": "trigger"})

    # Collect anomaly alerts
    for alert in anomaly_alerts:
        all_items.append({"message": alert, "source": "anomaly"})

    # Collect validated outbox messages
    for item in valid_items:
        all_items.append({"message": item["message"], "source": "outbox"})

    if not all_items:
        # Still mark validated outbox items as consumed even if nothing to deliver
        _mark_sent(validated_hashes=set(validated_hashes))
        return 0

    if delivery_mode == "webhook":
        webhook_url = cfg.get("webhook_url", "")
        if not webhook_url:
            print("ERROR: webhook_url not configured", file=__import__('sys').stderr)
            return 1
        all_delivered = _deliver_via_webhook(all_items, webhook_url)

    elif delivery_mode == "app_dm":
        token = _get_tenant_token(cfg.get("app_id", ""), cfg.get("app_secret", ""))
        if not token:
            print("ERROR: Failed to get Feishu tenant token", file=__import__('sys').stderr)
            return 1
        receive_id = cfg.get("receive_open_id", "")
        if not receive_id:
            print("ERROR: receive_open_id not configured", file=__import__('sys').stderr)
            return 1
        all_delivered = _deliver_via_app_dm(all_items, token, receive_id)

    else:
        print(f"ERROR: Unknown delivery_mode={delivery_mode}", file=__import__('sys').stderr)
        return 1

    # ── Step 4: Consume only validated outbox items (not trigger/anomaly alerts) ──
    if all_delivered:
        _mark_sent(validated_hashes=set(validated_hashes))
    else:
        return 1

    return 0


def _deliver_via_webhook(items: list[dict], webhook_url: str) -> bool:
    """Deliver messages via Feishu webhook. Returns True if all sent."""
    import sys as _sys
    for item in items:
        text = item["message"]
        payload = {"msg_type": "text", "content": {"text": text}}
        try:
            r = requests.post(webhook_url, json=payload, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("code", -1) != 0:
                logger.warning("_deliver_via_webhook: Feishu webhook error code=%s msg=%s",
                               data.get("code"), data.get("msg", ""))
                return False
        except Exception as e:
            logger.warning("_deliver_via_webhook: %s", e)
            return False
        __import__('time').sleep(0.5)
    return True


def _deliver_via_app_dm(items: list[dict], token: str, receive_open_id: str) -> bool:
    """Deliver messages via Feishu app DM API. Returns True if all sent."""
    import sys as _sys
    for item in items:
        text = item["message"]
        ok = _send_app_dm(token, receive_open_id, text)
        if not ok:
            return False
        __import__('time').sleep(0.5)
    return True


def main():
    parser = argparse.ArgumentParser(description="Bridge validator")
    parser.add_argument("--mode", choices=["validate", "consume", "deliver"], default="validate",
                        help="validate: check + output | consume: mark sent | deliver: validate+send+consume")
    args = parser.parse_args()

    if args.mode == "validate":
        raise SystemExit(run_validate())
    elif args.mode == "consume":
        raise SystemExit(run_consume())
    else:
        raise SystemExit(run_deliver())


if __name__ == "__main__":
    main()
