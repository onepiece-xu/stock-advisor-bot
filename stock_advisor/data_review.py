"""
数据驱动复盘模块 — 不靠 LLM 猜，用量价关系说话。

每次收盘后从 market.db 提取分钟数据，分析：
- 量价关系（谁在买、谁在卖）
- 日内走势形态（高开低走 / 探底回升 / 单边）
- 相对大盘强弱
- 关键支撑/阻力位（基于实际成交密集区）
- 信号准确度回溯
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


# ── 量价分类 ──────────────────────────────────────────

def classify_volume_price(
    price_change_pct: float,
    volume_change_pct: float,
    intraday_amplitude: float = 0,
) -> tuple[str, str]:
    """返回 (标签, 解释)。"""
    if price_change_pct > 2:
        if volume_change_pct > 20:
            return ("📈 放量上攻", "资金主动进场，量价配合好")
        elif volume_change_pct < -10:
            return ("⚠️ 缩量上涨", "反弹无量，持续性存疑")
        else:
            return ("🟢 温和上涨", "量价正常")
    elif price_change_pct > 0:
        if volume_change_pct > 30:
            return ("🔍 放量滞涨", "量大价不涨=有人在出货")
        else:
            return ("🟢 小幅收涨", "")
    elif price_change_pct < -3:
        if volume_change_pct > 20:
            return ("🔴 放量暴跌", "恐慌盘涌出，短期见底概率上升")
        elif volume_change_pct < -10:
            return ("🟡 缩量大跌", "卖压在衰减，但无人接盘")
        else:
            return ("🔴 大跌", f"跌幅{abs(price_change_pct):.1f}%")
    elif price_change_pct < 0:
        if intraday_amplitude > 5:
            return ("🟡 宽幅震荡收跌", "多空激烈博弈")
        else:
            return ("🟡 阴跌", f"跌{abs(price_change_pct):.1f}%，方向偏空")
    else:
        return ("➡️ 平盘", "无方向")


# ── 日内形态 ──────────────────────────────────────────

def classify_intraday_pattern(
    conn: sqlite3.Connection,
    code: str,
    trade_date: str,
) -> str:
    """分析日内走势形态。"""
    cursor = conn.execute(
        """SELECT quote_time, current_price, open_price, high_price, low_price
           FROM quotes
           WHERE code = ? AND DATE(quote_time) = ?
           ORDER BY quote_time""",
        (code, trade_date),
    )
    rows = cursor.fetchall()
    if len(rows) < 30:
        return "数据不足"

    first = rows[0]
    last = rows[-1]
    open_p = float(first[2] or first[1])
    close_p = float(last[1])
    high_p = max(float(r[3] or r[1]) for r in rows)
    low_p = min(float(r[4] or r[1]) for r in rows)
    amplitude = (high_p - low_p) / low_p * 100 if low_p else 0

    # 解析时间
    parsed_rows = []
    for r in rows:
        try:
            t = datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S") if ":" in str(r[0]) else datetime.strptime(str(r[0]), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                t = datetime.fromisoformat(str(r[0]))
            except ValueError:
                continue
        parsed_rows.append((t, float(r[1])))

    # 上午/下午分段
    am_prices = [p for t, p in parsed_rows if t.hour < 11 or (t.hour == 11 and t.minute <= 30)]
    pm_prices = [p for t, p in parsed_rows if t.hour >= 13]

    am_avg = sum(am_prices) / len(am_prices) if am_prices else open_p
    pm_avg = sum(pm_prices) / len(pm_prices) if pm_prices else close_p

    am_pm_ratio = (pm_avg - am_avg) / am_avg * 100 if am_avg else 0

    if close_p > open_p * 1.02:
        if am_pm_ratio > 0.5:
            return f"📈 高开高走（午后续强，振幅{amplitude:.1f}%）"
        else:
            return f"📈 高开后震荡收涨（振幅{amplitude:.1f}%）"
    elif close_p < open_p * 0.98:
        if am_pm_ratio < -0.5:
            return f"📉 低开低走（午后加速下跌，振幅{amplitude:.1f}%）"
        else:
            return f"📉 低开震荡收跌（振幅{amplitude:.1f}%）"
    elif close_p > open_p:
        if abs(am_pm_ratio) < 0.3:
            return f"➡️ 窄幅收涨（振幅{amplitude:.1f}%）"
        else:
            return f"📊 震荡收涨（振幅{amplitude:.1f}%）"
    elif close_p < open_p:
        return f"📊 震荡收跌（振幅{amplitude:.1f}%）"
    else:
        return f"➡️ 平收（振幅{amplitude:.1f}%）"


# ── 相对强弱 ──────────────────────────────────────────

def compute_relative_strength(
    conn: sqlite3.Connection,
    code: str,
    benchmark_code: str = "000001",
    lookback_days: int = 3,
) -> str:
    """与上证指数对比相对强弱。"""
    stock_changes: list[float] = []
    bench_changes: list[float] = []

    for days_ago in range(lookback_days, 0, -1):
        d = (datetime.now().date() - timedelta(days=days_ago)).isoformat()
        for target, lst in [(code, stock_changes), (benchmark_code, bench_changes)]:
            row = conn.execute(
                """SELECT current_price, open_price
                   FROM quotes WHERE code = ? AND DATE(quote_time) = ?
                   ORDER BY quote_time DESC LIMIT 1""",
                (target, d),
            ).fetchone()
            if row:
                chg = (float(row[0]) - float(row[1])) / float(row[1]) * 100 if row[1] else 0
                lst.append(chg)

    if len(stock_changes) < 2:
        return "数据不足"

    stock_avg = sum(stock_changes) / len(stock_changes)
    bench_avg = sum(bench_changes) / len(bench_changes) if bench_changes else 0
    diff = stock_avg - bench_avg

    if diff > 1:
        return f"🟢 强于大盘 +{diff:.1f}%（独立走强）"
    elif diff < -1:
        return f"🔴 弱于大盘 {diff:.1f}%（系统性偏弱）"
    else:
        return f"➡️ 与大盘同步（{diff:+.1f}%）"


# ── 支撑/阻力位（成交量加权） ──────────────────────────

def find_key_levels(
    conn: sqlite3.Connection,
    code: str,
    lookback_days: int = 5,
) -> dict:
    """基于最近 N 天的成交量分布找支撑/阻力。"""
    cursor = conn.execute(
        """SELECT current_price, volume_shares
           FROM quotes
           WHERE code = ? AND DATE(quote_time) >= ?
           ORDER BY quote_time""",
        (code, (datetime.now().date() - timedelta(days=lookback_days)).isoformat()),
    )
    rows = cursor.fetchall()
    if not rows:
        return {}

    # 价格分桶（0.5元一档）
    buckets: dict[int, tuple[float, float]] = defaultdict(lambda: (0.0, 0.0))
    for price, vol in rows:
        p = float(price)
        bucket_key = int(p * 2)  # 0.5元分辨率
        total_vol, total_price_weight = buckets[bucket_key]
        buckets[bucket_key] = (total_vol + float(vol or 0), total_price_weight + p * float(vol or 0))

    # 找成交量最大的两个价格区间
    sorted_buckets = sorted(buckets.items(), key=lambda x: x[1][0], reverse=True)
    if len(sorted_buckets) < 2:
        return {}

    vwap_1 = sorted_buckets[0][1][1] / sorted_buckets[0][1][0] if sorted_buckets[0][1][0] else 0
    vwap_2 = sorted_buckets[1][1][1] / sorted_buckets[1][1][0] if sorted_buckets[1][1][0] else 0

    return {
        "max_vol_zone": round(vwap_1, 2),
        "second_vol_zone": round(vwap_2, 2),
    }


# ── 信号回溯 ──────────────────────────────────────────

def backtest_yesterdays_signal(
    conn: sqlite3.Connection,
    code: str,
    name: str,
    yesterday: str,
    today: str,
) -> str:
    """检查昨天的交易信号今天是否验证，并记录准确度。"""
    # 找昨天的推荐动作
    signal_row = conn.execute(
        """SELECT ds.action, ds.trade_advice
           FROM decision_signals ds
           WHERE ds.code = ? AND DATE(ds.created_at) = ?
           ORDER BY ds.created_at DESC LIMIT 1""",
        (code, yesterday),
    ).fetchone()

    if not signal_row:
        return "昨日无信号"

    action, advice = signal_row
    action = action or "hold"

    # 今天的实际涨跌
    price_row = conn.execute(
        """SELECT current_price, open_price
           FROM quotes WHERE code = ? AND DATE(quote_time) = ?
           ORDER BY quote_time DESC LIMIT 1""",
        (code, today),
    ).fetchone()

    if not price_row:
        return "今日无数据"

    chg = (float(price_row[0]) - float(price_row[1])) / float(price_row[1]) * 100 if price_row[1] else 0

    # 判定准确度 verdict
    if action in ("buy",):
        verdict = "correct" if chg > 0 else "wrong"
        if chg > 0:
            result = f"✅ 买入信号正确（今日涨{chg:+.1f}%）"
        else:
            result = f"❌ 买入信号被证伪（今日跌{chg:+.1f}%）"
    elif action in ("reduce", "sell"):
        verdict = "correct" if chg < 0 else "wrong"
        if chg < 0:
            result = f"✅ 卖出信号正确（今日跌{chg:+.1f}%）"
        else:
            result = f"⚠️ 卖出信号可能误判（今日涨{chg:+.1f}%）"
    elif action == "avoid":
        verdict = "correct" if chg < -1 else ("wrong" if chg > 1 else "ambiguous")
        if chg < -1:
            result = f"✅ 回避正确（今日跌{chg:+.1f}%）"
        elif chg > 1:
            result = f"⚠️ 回避错失机会（今日涨{chg:+.1f}%）"
        else:
            result = f"➡️ 回避合理（涨跌{chg:+.1f}%）"
    else:
        verdict = "ambiguous"
        result = f"持有信号（今日{chg:+.1f}%）"

    # 记录到信号准确度追踪
    save_signal_accuracy(today, code, name, action, chg, verdict)

    return result


# ── 信号准确度追踪 ──────────────────────────────────────

def save_signal_accuracy(
    date: str,
    code: str,
    name: str,
    yesterday_action: str,
    today_change_pct: float,
    verdict: str,
) -> None:
    """将单次信号预测 vs 实际结果记录到 data/signal_accuracy.jsonl。

    每行一条 JSON 记录，格式：
    {"date":"2026-05-18","code":"601698","name":"中国卫通","yesterday_action":"reduce","today_change_pct":1.08,"verdict":"wrong"}
    """
    import json

    record = {
        "date": date,
        "code": code,
        "name": name,
        "yesterday_action": yesterday_action,
        "today_change_pct": round(today_change_pct, 2),
        "verdict": verdict,
    }
    acc_path = Path(__file__).resolve().parent.parent / "data" / "signal_accuracy.jsonl"
    acc_path.parent.mkdir(parents=True, exist_ok=True)
    with open(acc_path, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def get_signal_accuracy_stats(days: int = 10) -> dict[str, dict[str, float]]:
    """统计近 N 天各动作的信号准确率。

    Args:
        days: 统计最近多少天的数据（默认 10）。

    Returns:
        dict: 按 action 分组的统计数据，以及 overall 汇总。
              例：{"buy": {"total": 5, "correct": 3, "rate": 0.6}, ...,
                    "overall": {"total": 12, "correct": 8, "rate": 0.67}}
    """
    import json

    acc_path = Path(__file__).resolve().parent.parent / "data" / "signal_accuracy.jsonl"
    if not acc_path.exists():
        return {}

    cutoff = (datetime.now().date() - timedelta(days=days)).isoformat()
    action_stats: dict[str, dict] = defaultdict(lambda: {"total": 0, "correct": 0})

    with open(acc_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("date", "") < cutoff:
                continue
            act = record.get("yesterday_action", "unknown")
            verdict = record.get("verdict", "unknown")
            action_stats[act]["total"] += 1
            if verdict == "correct":
                action_stats[act]["correct"] += 1

    # 计算准确率
    for stats in action_stats.values():
        stats["rate"] = round(stats["correct"] / stats["total"], 3) if stats["total"] else 0.0

    total_all = sum(s["total"] for s in action_stats.values())
    correct_all = sum(s["correct"] for s in action_stats.values())
    action_stats["overall"] = {
        "total": total_all,
        "correct": correct_all,
        "rate": round(correct_all / total_all, 3) if total_all else 0.0,
    }

    return dict(action_stats)


# ── 主入口：生成数据复盘 ──────────────────────────────

def generate_data_review(
    db_path: str | Path,
    stocks: list[dict],
    trade_date: str | None = None,
    benchmark_code: str = "000001",
) -> tuple[str, str]:
    """
    生成数据驱动的每日复盘。

    Returns:
        (full_markdown, mobile_summary) — full_markdown 存文档，mobile_summary 推送飞书。
    """
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # 前一个交易日
    cursor = conn.execute(
        "SELECT DISTINCT DATE(quote_time) as d FROM quotes WHERE provider = 'sina_minute' AND d < ? ORDER BY d DESC LIMIT 2",
        (trade_date,),
    )
    prev_dates = [r["d"] for r in cursor.fetchall()]
    prev_date = prev_dates[0] if prev_dates else None

    lines = [
        f"## {trade_date.replace('-', '.')}",
        "",
        "### 📊 数据复盘",
        "",
    ]

    total_pnl = 0.0
    total_value = 0.0

    # Collect per-stock summary for mobile push
    mobile_lines: list[str] = []
    alerts: list[str] = []

    for stock in stocks:
        code = stock["code"]
        name = stock["name"]
        cost = float(stock.get("cost", 0))
        shares = int(stock.get("shares", 0))

        # ── 今日数据 ──
        today_open_row = conn.execute(
            "SELECT open_price FROM quotes WHERE code=? AND DATE(quote_time)=? ORDER BY quote_time LIMIT 1",
            (code, trade_date),
        ).fetchone()
        today_close_row = conn.execute(
            "SELECT current_price, high_price, low_price, volume_shares FROM quotes WHERE code=? AND DATE(quote_time)=? ORDER BY quote_time DESC LIMIT 1",
            (code, trade_date),
        ).fetchone()

        if not today_close_row:
            lines.append(f"**{name}**：今日无数据")
            lines.append("")
            continue

        today_open = float(today_open_row["open_price"]) if today_open_row else 0
        today_close = float(today_close_row["current_price"])
        today_high = float(today_close_row["high_price"])
        today_low = float(today_close_row["low_price"])
        today_vol = float(today_close_row["volume_shares"])
        amplitude = (today_high - today_low) / today_low * 100 if today_low else 0
        day_chg = (today_close - today_open) / today_open * 100 if today_open else 0

        # ── 昨日数据 ──
        yesterday_vol = None
        yesterday_close = None
        if prev_date:
            yc_row = conn.execute(
                "SELECT current_price, volume_shares FROM quotes WHERE code=? AND DATE(quote_time)=? ORDER BY quote_time DESC LIMIT 1",
                (code, prev_date),
            ).fetchone()
            if yc_row:
                yesterday_close = float(yc_row["current_price"])
                yesterday_vol = float(yc_row["volume_shares"])

        # ── 量价分析 ──
        if yesterday_close and yesterday_vol:
            close_chg = (today_close - yesterday_close) / yesterday_close * 100
            vol_chg = (today_vol - yesterday_vol) / yesterday_vol * 100 if yesterday_vol else 0
        else:
            close_chg = day_chg
            vol_chg = 0

        vp_label, vp_reason = classify_volume_price(close_chg, vol_chg, amplitude)

        # ── 日内形态 ──
        intraday = classify_intraday_pattern(conn, code, trade_date)

        # ── 相对强弱 ──
        rel_str = compute_relative_strength(conn, code, benchmark_code)

        # ── 关键价位 ──
        key_levels = find_key_levels(conn, code)

        # ── 信号回溯 ──
        signal_check = ""
        if prev_date:
            signal_check = backtest_yesterdays_signal(conn, code, name, prev_date, trade_date)

        # ── 盈亏 ──
        pnl = (today_close - cost) * shares if cost else 0
        pnl_pct = (today_close - cost) / cost * 100 if cost else 0
        total_pnl += pnl
        total_value += today_close * shares

        # ── 输出 ──
        emoji = "🟢" if pnl_pct > 0 else ("🔴" if pnl_pct < -5 else "🟡")

        lines.append(f"**{emoji} {name} {code}**")
        lines.append(f"- 收盘：{today_close:.2f}  |  涨跌：{close_chg:+.2f}%  |  量变：{vol_chg:+.0f}%")
        lines.append(f"- 量价：{vp_label} — {vp_reason}")
        lines.append(f"- 走势：{intraday}")
        lines.append(f"- 强弱：{rel_str}")
        if key_levels:
            lines.append(
                f"- 关键位：成交量密集区 {key_levels['max_vol_zone']}"
                + (f"，次密集 {key_levels['second_vol_zone']}" if key_levels.get("second_vol_zone") else "")
            )
        if signal_check:
            lines.append(f"- 信号回溯：{signal_check}")
        lines.append(f"- 持仓：{shares}股 × {today_close:.2f} = {today_close*shares:.0f}元  |  {emoji} {pnl:+.0f}元 ({pnl_pct:+.1f}%)")
        lines.append("")

        # ── 手机推送摘要 ──
        mobile_lines.append(
            f"{emoji} {name} {close_chg:+.1f}% | {vp_label.split()[0]} | {intraday.split('（')[0]} | {pnl:+.0f}元"
        )
        if pnl_pct < -20:
            alerts.append(f"🔴 {name}：深套{pnl_pct:.1f}%，反弹至{today_close*1.03:.2f}减仓")
        elif pnl_pct < -5:
            alerts.append(f"🟡 {name}：浮亏{pnl_pct:.1f}%，止损{today_close*0.93:.2f}")
        elif pnl_pct > 5:
            # 移动止盈：用今日高点做峰值，计算回撤触发价（替代固定 cost*1.1 一刀切）
            if today_high > cost:
                peak_profit_pct = (today_high - cost) / cost * 100
                if peak_profit_pct >= 20:
                    dd = 0.10
                elif peak_profit_pct >= 10:
                    dd = 0.08
                else:
                    dd = 0.05
                tp_trigger = today_high * (1 - dd)
                if today_close <= tp_trigger:
                    alerts.append(f"🎯 {name}：移动止盈触发！峰值{today_high:.2f}回撤{dd*100:.0f}%，现价{today_close:.2f}")
                else:
                    alerts.append(f"🟢 {name}：浮盈{pnl_pct:.1f}%，移动止盈{tp_trigger:.2f}（峰值回撤{dd*100:.0f}%）")
            else:
                alerts.append(f"🟢 {name}：浮盈{pnl_pct:.1f}%，关注移动止盈")

    # ── 汇总 ──
    lines.append("### 📋 复盘结论")
    lines.append("")

    # 自动生成结论
    worst = None
    best = None
    for stock in stocks:
        pnl_pct = (float(stock.get("current_price", 0)) - float(stock.get("cost", 0))) / float(stock.get("cost", 1)) * 100
        if worst is None or pnl_pct < worst[1]:
            worst = (stock["name"], pnl_pct)
        if best is None or pnl_pct > best[1]:
            best = (stock["name"], pnl_pct)

    lines.append(f"**总资产**：约 {total_value + (50000 - sum(float(s.get('cost',0))*int(s.get('shares',0)) for s in stocks)):.0f} 元")
    lines.append(f"**总盈亏**：{total_pnl:+.0f} 元")
    lines.append("")

    # 需要处理的
    alerts = []
    for stock in stocks:
        cost = float(stock.get("cost", 0))
        if cost <= 0:
            continue
        # Get current price and today's high
        row = conn.execute(
            "SELECT current_price, high_price FROM quotes WHERE code=? AND DATE(quote_time)=? ORDER BY quote_time DESC LIMIT 1",
            (stock["code"], trade_date),
        ).fetchone()
        if not row:
            continue
        price = float(row["current_price"])
        today_high2 = float(row["high_price"])
        pnl_pct = (price - cost) / cost * 100

        if pnl_pct < -20:
            alerts.append(f"- 🔴 **{stock['name']}**：深套{pnl_pct:.1f}%，明日反弹至{price*1.03:.2f}附近减仓")
        elif pnl_pct < -5:
            alerts.append(f"- 🟡 **{stock['name']}**：浮亏{pnl_pct:.1f}%，设止损{price*0.93:.2f}")
        elif pnl_pct > 5:
            # 移动止盈替代固定止盈价
            if today_high2 > cost:
                peak_p = (today_high2 - cost) / cost * 100
                if peak_p >= 20:
                    dd2 = 0.10
                elif peak_p >= 10:
                    dd2 = 0.08
                else:
                    dd2 = 0.05
                tp2 = today_high2 * (1 - dd2)
                if price <= tp2:
                    alerts.append(f"- 🎯 **{stock['name']}**：移动止盈触发！峰值{today_high2:.2f}回撤{dd2*100:.0f}%")
                else:
                    alerts.append(f"- 🟢 **{stock['name']}**：浮盈{pnl_pct:.1f}%，移动止盈{tp2:.2f}（峰值回撤{dd2*100:.0f}%）")
            else:
                alerts.append(f"- 🟢 **{stock['name']}**：浮盈{pnl_pct:.1f}%，关注移动止盈")

    if alerts:
        lines.append("**明日操作要点：**")
        lines.extend(alerts)
        lines.append("")
    else:
        lines.append("**明日操作要点**：无紧急操作，按计划执行")
        lines.append("")

    # ── 信号准确度统计 ──
    accuracy_stats = get_signal_accuracy_stats(days=10)
    if accuracy_stats:
        lines.append("### 📈 信号准确度统计（近10天）")
        lines.append("")
        lines.append("| 动作 | 总次数 | 正确 | 准确率 |")
        lines.append("|------|--------|------|--------|")
        action_labels = {"buy": "🟢 买入", "reduce": "🔴 减仓", "sell": "🔴 卖出", "avoid": "🟡 回避", "hold": "➡️ 持有", "unknown": "❓ 未知"}
        for act, stats in sorted(accuracy_stats.items(), key=lambda x: x[1].get("rate", 0), reverse=True):
            if act == "overall":
                continue
            label = action_labels.get(act, act)
            rate_pct = f"{stats['rate'] * 100:.0f}%"
            lines.append(f"| {label} | {stats['total']} | {stats['correct']} | {rate_pct} |")
        # 汇总行
        overall = accuracy_stats.get("overall", {})
        if overall:
            lines.append(f"| **合计** | **{overall['total']}** | **{overall['correct']}** | **{overall['rate'] * 100:.0f}%** |")
        lines.append("")

    conn.close()

    # ── 手机推送摘要（短，不会被截断） ──
    mobile = [
        f"📊 {trade_date.replace('-', '.')} 数据复盘",
        f"总资产 {total_value:.0f} | 总盈亏 {total_pnl:+.0f}",
        "",
    ]
    mobile.extend(mobile_lines)
    if alerts:
        mobile.append("")
        mobile.append("⚠️ 明日要点：")
        mobile.extend(alerts)

    return "\n".join(lines), "\n".join(mobile)
