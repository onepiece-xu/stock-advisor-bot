#!/usr/bin/env python3
"""Dashboard — one-command trading view for Hermes to make decisions.

Usage:
  python3 -m stock_advisor.cli dashboard --config config.yaml

Outputs all information needed for trading judgment in one shot:
  - Time / session status
  - Portfolio snapshot with pnl, cost basis, market value
  - Active triggers with source tags
  - Live quotes with technical indicators (MA/RSI/volume/amplitude)
  - Cross-stock comparison (strongest → weakest)
  - Briefing summary
  - News / catalyst hints
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

logger = logging.getLogger(__name__)


def _tencent_quote_raw(codes: list[str]) -> dict[str, dict]:
    """Fetch raw Tencent quotes and return parsed fields."""
    key = ",".join(codes)
    url = f"https://qt.gtimg.cn/q={key}"
    try:
        result = subprocess.run(
            ["curl", "-s", url],
            capture_output=True, text=True, timeout=15,
        )
        raw = result.stdout
        # Handle GBK — qt.gtimg.cn returns GBK, sometimes with latin1 pass-through
        for enc in ("utf-8", "gbk", "gb2312", "latin1"):
            try:
                trial = raw.encode("latin1").decode(enc)
                if "~" in trial and len(trial) > 50:
                    raw = trial
                    break
            except Exception:
                continue
    except Exception:
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
        payload = payload.strip('";\n ')
        parts = payload.split("~")
        if len(parts) < 40:
            continue
        try:
            name = parts[1]
            code = parts[2]
            current = Decimal(parts[3])
            prev_close = Decimal(parts[4])
            open_price = Decimal(parts[5])
            high = Decimal(parts[33])
            low = Decimal(parts[34])
            chg_pct = Decimal(parts[32])
            volume = Decimal(parts[6])
            amount = Decimal(parts[37]) if len(parts) > 37 else Decimal("0")
            amplitude = ((high - low) / prev_close * 100).quantize(Decimal("0.01")) if prev_close > 0 else Decimal("0")
            out[code] = {
                "name": name,
                "code": code,
                "current": current,
                "prev_close": prev_close,
                "open": open_price,
                "high": high,
                "low": low,
                "chg_pct": chg_pct,
                "volume": volume,
                "amount": amount,
                "amplitude": amplitude,
            }
        except Exception:
            continue
    return out


def _try_fetch_tech(code: str, data_dir: Path) -> dict:
    """Try to pull latest observation for a stock from market.db."""
    try:
        import sqlite3
        db_path = data_dir / "market.db"
        if not db_path.exists():
            return {}
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM observations WHERE symbol = ? ORDER BY obs_time DESC LIMIT 1",
            (f"sh{code}" if code.startswith("6") else f"sz{code}",),
        ).fetchone()
        conn.close()
        if not row:
            return {}
        return {
            "ma5": row["ma5"],
            "ma15": row["ma15"],
            "ma60": row["ma60"],
            "rsi14": row["rsi14"],
            "volume_ratio": row["volume_ratio"],
            "bias_to_ma60": row["bias_to_ma60"],
            "amplitude": row.get("intraday_amplitude_pct"),
        }
    except Exception:
        return {}


def _load_briefing(data_dir: Path) -> dict:
    bp = data_dir / "briefing" / "latest.json"
    if not bp.exists():
        return {}
    try:
        return json.loads(bp.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _build_news_section(codes: list[str]) -> str:
    """Minimal news check — just flag if there's anything to know."""
    lines = []
    for code in codes:
        lines.append(f"  {code}: 未拉取（已暂停独立资讯推送）")
    return "\n".join(lines) if lines else "  无"


def _compare_stocks(holdings: list[dict], tech: dict[str, dict]) -> list[dict]:
    """Rank holdings by composite strength for cross-stock comparison."""
    scored = []
    for h in holdings:
        code = h["code"]
        t = tech.get(code, {})
        score = 0
        reasons = []
        pnl = h.get("pnl_pct", Decimal("0"))
        if pnl > 0:
            score += 30
            reasons.append("浮盈")
        elif pnl > -5:
            score += 15
            reasons.append("浅亏")
        else:
            reasons.append("浮亏")
        ma60_bias = t.get("bias_to_ma60")
        if ma60_bias is not None:
            bias = Decimal(str(ma60_bias))
            if bias > 1:
                score += 20
                reasons.append("MA60上方")
            elif bias > -1:
                score += 10
                reasons.append("MA60附近")
        rsi = t.get("rsi14")
        if rsi is not None:
            rsi_val = Decimal(str(rsi))
            if rsi_val > 60:
                score += 15
                reasons.append("RSI偏强")
            elif rsi_val < 30:
                score -= 10
                reasons.append("RSI超卖")
        vol_r = t.get("volume_ratio")
        if vol_r is not None and Decimal(str(vol_r)) > Decimal("1.5"):
            score += 10
            reasons.append("放量")
        scored.append({"code": code, "name": h["name"], "score": score, "pnl": pnl, "reasons": reasons})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def run_dashboard(config_path: str) -> None:
    from stock_advisor.config import require_valid_config
    from stock_advisor.market_hours import MARKET_TZ, is_a_share_trading_time
    from stock_advisor.trading_state import TradingState
    from stock_advisor.state_builder import build_trading_state
    from stock_advisor.market_hours import next_session_str as market_next_session

    config = require_valid_config(config_path)
    now = datetime.now(MARKET_TZ)
    is_trading = is_a_share_trading_time(now)

    print("=" * 60)
    print(f"📊 TRADING DASHBOARD  {now.strftime('%Y-%m-%d %H:%M %A')}")
    print(f"   交易时段: {'是' if is_trading else '否'} | 下次开盘: {market_next_session(now)}")
    print("=" * 60)

    # ── 1. Unified state ──
    briefing_path = Path("data/briefing/latest.json")
    state = build_trading_state(config.snapshot_path, config.trading_plan.path, briefing_path)

    print(f"\n💰 总资产 {state.total_assets:.0f} | 现金 {state.cash:.0f}")

    # ── 2. Holdings ──
    print("\n── 持仓 ──")
    holdings_list = []
    for h in state.holdings:
        holdings_list.append({
            "code": h.code,
            "name": h.name,
            "quantity": h.quantity,
            "cost_price": float(h.cost_price),
            "current_price": float(h.current_price),
            "pnl_pct": float(h.pnl_pct),
            "market_value": float(h.market_value),
        })
        print(f"  {h.name}({h.code}) {h.quantity}股 | 成本{h.cost_price} | 现价{h.current_price} | {h.pnl_pct:+.1f}% | 市值{h.market_value:.0f}")

    # ── 3. Live quotes with tech ──
    codes = [h.code for h in state.holdings]
    quotes = _tencent_quote_raw(codes)
    print("\n── 实时行情 + 技术指标 ──")
    tech_map = {}
    for code in codes:
        q = quotes.get(code)
        tech = _try_fetch_tech(code, Path("data"))
        tech_map[code] = tech
        if q:
            ma60_b = tech.get("bias_to_ma60", "?")
            rsi = tech.get("rsi14", "?")
            vr = tech.get("volume_ratio", "?")
            print(
                f"  {q['name']}({code}) "
                f"{q['current']} ({q['chg_pct']:+.2f}%) "
                f"高{q['high']} 低{q['low']} 振{q['amplitude']}% | "
                f"偏离MA60:{ma60_b} RSI:{rsi} 量比:{vr}"
            )
        else:
            print(f"  {code}: 实时行情获取失败")

    # ── 4. Cross-stock comparison ──
    ranked = _compare_stocks(holdings_list, tech_map)
    print("\n── 强弱排序 ──")
    for rank, item in enumerate(ranked, 1):
        emoji = {1: "🏆", 2: "🥈", 3: "🥉"}.get(rank, "")
        print(f"  {emoji} {rank}. {item['name']}({item['code']}) | {item['pnl']:+.1f}% | {' / '.join(item['reasons'])}")

    # ── 5. Active triggers ──
    print("\n── 活跃触发单 ──")
    for inst in state.active_instructions:
        print(f"  {inst.code} {inst.name}: {inst.action} {inst.quantity}股 @ {inst.trigger_low}-{inst.trigger_high}")

    # ── 6. Briefing key lines ──
    if state.briefing_summary:
        summary = state.briefing_summary.strip()
        lines = summary.split("\n")
        print("\n── 盘前摘要 ──")
        for line in lines:
            if line.strip():
                print(f"  {line}")

    # ── 7. News ──
    print("\n── 资讯 ──")
    print(_build_news_section(codes))

    # ── 8. Current unified verdict ──
    print("\n" + "=" * 60)
    print("🎯 统一判断")
    active_codes = {inst.code for inst in state.active_instructions}
    for h in state.holdings:
        briefing_hold = False
        for line in state.briefing_summary.split("\n"):
            if h.name in line and ("强势持有" in line or "涨停" in line and "不卖" in line):
                briefing_hold = True
                break
        if briefing_hold:
            print(f"   {h.name}: HOLD（简报定调：持有不卖）")
        else:
            insts = [i for i in state.active_instructions if i.code == h.code]
            if insts:
                for i in insts:
                    print(f"   {i.name}: {i.action.upper()} {i.quantity}股 @ {i.trigger_low}-{i.trigger_high}")
            else:
                print(f"   {h.name}: HOLD")
    print("=" * 60)


if __name__ == "__main__":
    run_dashboard(sys.argv[2] if len(sys.argv) > 2 else "config.yaml")
