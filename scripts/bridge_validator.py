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
  python3 scripts/bridge_validator.py --mode validate   # validate & output
  python3 scripts/bridge_validator.py --mode consume    # mark sent after delivery
"""

import argparse
import json
import os
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import requests

from stock_advisor.config import load_config

REPO = Path(__file__).resolve().parent.parent
OUTBOX_PATH = REPO / "data" / "outbox.jsonl"
SNAPSHOT_PATH = REPO / "portfolio-snapshot.json"
SKIPPED_LOG = REPO / "data" / "bridge_skipped.log"
STATE_PATH = REPO / "data" / "bridge_validator_state.json"
DB_PATH = REPO / "data" / "market.db"

_KLINE_CACHE: dict[str, list[dict]] = {}
_INTRADAY_CACHE: dict[str, dict] = {}


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
    except Exception:
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

    except Exception:
        pass

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
    except Exception:
        pass
    return holdings


# ──────────────────────────────────────────────
#  Message parsing
# ──────────────────────────────────────────────

def extract_code_and_action(msg: str) -> tuple[str | None, str | None]:
    """Extract 6-digit stock code and action (buy/sell) from message text."""
    code_match = re.search(r'\b(\d{6})\b', msg)
    code = code_match.group(1) if code_match else None
    action = None
    if re.search(r'买入|buy', msg, re.IGNORECASE):
        if not re.search(r'不建议买入|不买入|暂不买入|不推荐买入|buy\s+signal\s*[：:]?\s*no', msg, re.IGNORECASE):
            action = 'buy'
    if re.search(r'卖出|清仓|减仓|reduce|avoid|sell', msg, re.IGNORECASE):
        if not re.search(r'不建议卖出|不卖出|暂不卖出', msg):
            action = 'sell'
    return code, action


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

    # New stock, not in holdings → allow
    if not holding:
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
    return {"validated_count": 0, "total_pending": 0, "validated_at": None}


def save_state(validated_count: int, total_pending: int) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({
        "validated_count": validated_count,
        "total_pending": total_pending,
        "validated_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False))


# ──────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────

def run_validate() -> int:
    """Validate outbox entries. Output passing messages to stdout. Returns exit code."""
    if not OUTBOX_PATH.exists():
        return 0

    holdings = load_holdings()
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

    valid_messages: list[str] = []
    skipped_reasons: list[str] = []
    skipped_count = 0

    for item in pending[:10]:  # Process max 10 per tick
        should_deliver, reason = validate_entry(item, holdings)
        if should_deliver:
            valid_messages.append(item["message"])
        else:
            skipped_count += 1
            code, _ = extract_code_and_action(item.get("message", ""))
            pnl = holdings.get(code, {}).get("pnl_pct", 0) if code else 0
            skipped_reasons.append(
                f"SKIP {code or '???'}: {item.get('title','?')[:40]} ({reason})"
            )

    # Log skipped
    if skipped_reasons:
        SKIPPED_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(SKIPPED_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().isoformat()}] Validated {len(pending[:10])} pending, "
                    f"passed {len(valid_messages)}, skipped {skipped_count}:\n")
            for s in skipped_reasons:
                f.write(f"  {s}\n")

    # Save state so consume knows how many to mark
    total_pending = len(pending)
    validated_count = len(valid_messages)
    save_state(validated_count, total_pending)

    # Output validated messages
    if not valid_messages:
        # Still need to consume the skipped ones
        save_state(0, min(10, total_pending))  # Let consume clean up skipped
        return 0

    for msg in valid_messages:
        print(msg)
        print("---")

    return 0


def _mark_sent(limit: int = 10) -> int:
    """Mark up to `limit` unsent outbox entries as sent. Returns count marked."""
    if not OUTBOX_PATH.exists():
        return 0

    import fcntl
    fd = os.open(str(OUTBOX_PATH), os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
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
            if not item.get("sent") and marked < limit:
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
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    return marked


def run_consume() -> int:
    """Mark the previously-validated batch as sent."""
    count = _mark_sent(limit=10)
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
        data = r.json()
        return data.get("tenant_access_token")
    except Exception:
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
            if r.status_code != 200:
                return False
        except Exception:
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
    except Exception:
        pass
    return None


def run_deliver() -> int:
    """Validate + deliver + consume in one shot. For cron bridge use."""
    # 1. Validate
    if not OUTBOX_PATH.exists():
        return 0

    holdings = load_holdings()
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
    skipped_count = 0
    for item in pending[:10]:
        msg = item.get("message", "")
        should_deliver, reason = validate_entry(item, holdings)
        if should_deliver:
            valid_items.append(item)
        else:
            skipped_count += 1
            code, _ = extract_code_and_action(msg)
            _log_skip(code, item, reason)

    if skipped_count:
        _log_batch(len(pending[:10]), len(valid_items), skipped_count)

    if not valid_items:
        _mark_sent(limit=10)
        return 0

    # 2. Get delivery config
    cfg = _load_app_config()
    if not cfg:
        print("ERROR: Cannot load Feishu config from config.yaml", file=__import__('sys').stderr)
        return 1

    delivery_mode = cfg.get("delivery_mode", "webhook")

    # 3. Deliver based on mode
    all_delivered = True

    if delivery_mode == "webhook":
        webhook_url = cfg.get("webhook_url", "")
        if not webhook_url:
            print("ERROR: webhook_url not configured", file=__import__('sys').stderr)
            return 1
        all_delivered = _deliver_via_webhook(valid_items, webhook_url)

    elif delivery_mode == "app_dm":
        token = _get_tenant_token(cfg.get("app_id", ""), cfg.get("app_secret", ""))
        if not token:
            print("ERROR: Failed to get Feishu tenant token", file=__import__('sys').stderr)
            return 1
        receive_id = cfg.get("receive_open_id", "")
        if not receive_id:
            print("ERROR: receive_open_id not configured", file=__import__('sys').stderr)
            return 1
        all_delivered = _deliver_via_app_dm(valid_items, token, receive_id)

    else:
        print(f"ERROR: Unknown delivery_mode={delivery_mode}", file=__import__('sys').stderr)
        return 1

    # 4. Consume only if all delivered
    if all_delivered:
        _mark_sent(limit=10)
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
            if r.status_code != 200:
                return False
        except Exception:
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


def _log_skip(code: str | None, item: dict, reason: str) -> None:
    pass  # logged in batch below


def _log_batch(total: int, passed: int, skipped: int) -> None:
    SKIPPED_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(SKIPPED_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n[{datetime.now().isoformat()}] Validated {total} pending, "
                f"passed {passed}, skipped {skipped}\n")


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
