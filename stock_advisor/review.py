from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import AppConfig
from .habit_learning import build_trading_habit_profile
from .market_hours import MARKET_TZ, next_session_str
from .portfolio import load_snapshot as load_portfolio_snapshot
from .storage import connect_db, fetch_daily_review_snapshot, fetch_latest_trade_date
from .trading_plan import check_stale_triggers, load_triggers
from .logging_utils import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class ReviewArtifact:
    trade_date: date
    title: str
    body: str
    saved_path: Path


def build_close_review(config: AppConfig, *, trade_date: date | None = None) -> ReviewArtifact:
    if trade_date is None:
        trade_date = datetime.now(MARKET_TZ).date()
    conn = connect_db(config.storage.sqlite_path)
    requested_trade_date = trade_date
    items = fetch_daily_review_snapshot(conn, requested_trade_date.isoformat())
    if not items:
        latest_trade_date = fetch_latest_trade_date(conn)
        if latest_trade_date:
            trade_date = date.fromisoformat(latest_trade_date)
            items = fetch_daily_review_snapshot(conn, latest_trade_date)
    title = f"收盘复盘 {trade_date.isoformat()}"
    body = _render_review_body(config, trade_date, items, requested_trade_date=requested_trade_date)
    saved_path = _save_review(config.review.data_dir, trade_date, body)
    return ReviewArtifact(trade_date=trade_date, title=title, body=body, saved_path=saved_path)


def should_send_close_review_now(config: AppConfig, *, now: datetime | None = None) -> bool:
    if not config.review.enabled or not config.review.auto_notify:
        return False
    if now is None:
        now = datetime.now(MARKET_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=MARKET_TZ)
    else:
        now = now.astimezone(MARKET_TZ)
    if now.weekday() >= 5:
        return False
    cutoff = time(config.review.send_after_hour, config.review.send_after_minute)
    return now.time() >= cutoff


def already_sent_close_review(config: AppConfig, trade_date: date) -> bool:
    state = _load_review_state(config.review.data_dir)
    return state.get("last_sent_trade_date") == trade_date.isoformat()


def mark_close_review_sent(config: AppConfig, trade_date: date) -> None:
    state_path = _review_state_path(config.review.data_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "last_sent_trade_date": trade_date.isoformat(),
                "updated_at": datetime.now(MARKET_TZ).isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _render_review_body(config: AppConfig, trade_date: date, items: list[dict], *, requested_trade_date: date) -> str:
    conn = connect_db(config.storage.sqlite_path)
    trading_habit_profile = build_trading_habit_profile(conn)
    lines = [f"【收盘复盘】{trade_date.isoformat()}"]
    if trade_date != requested_trade_date:
        lines.append(f"说明: 当日暂无落库行情，已回退到最近交易日 {trade_date.isoformat()}")
    if not items:
        lines.append("今日暂无落库行情数据，未生成复盘明细。")
        lines.append("建议确认 monitor-daemon 是否正常运行。")
        return "\n".join(lines)

    scores = [Decimal(str(item["score"])) for item in items if item["score"] is not None]
    avg_score = _avg(scores)
    positive = max(items, key=lambda item: item["change_percent"])
    negative = min(items, key=lambda item: item["change_percent"])
    buy_like = [item["code"] for item in items if item["action"] == "buy"]
    reduce_like = [item["code"] for item in items if item["action"] in {"reduce", "avoid"}]

    lines.extend(
        [
            f"覆盖标的: {len(items)}",
            f"平均分: {_fmt_decimal(avg_score)}" if avg_score is not None else "平均分: N/A",
            f"偏强: {positive['code']} {positive['name']} {_signed(positive['change_percent'])}%",
            f"偏弱: {negative['code']} {negative['name']} {_signed(negative['change_percent'])}%",
            f"关注买点: {', '.join(buy_like) if buy_like else '暂无'}",
            f"优先减仓: {', '.join(reduce_like) if reduce_like else '暂无'}",
            "",
            "【标的复盘】",
        ]
    )

    for item in items:
        lines.append(
            f"- {item['code']} {item['name']} | 收盘 {_fmt_float(item['current_price'])} | 涨跌 {_signed(item['change_percent'])}% | 动作 {item['action']} | 评分 {_fmt_optional(item['score'])}"
        )
        lines.append(f"  状态 {item['regime']} / {item['confidence']} / {item['signal_level']}")
        if item["trade_advice"]:
            lines.append(f"  建议 {item['trade_advice']} | 仓位 {item['trade_size_hint']}")
        if item["entry_note"]:
            lines.append(f"  处理 {item['entry_note']}")
        reason = "；".join(item["rationale"][:2]) if item["rationale"] else "暂无明显理由"
        lines.append(f"  理由 {reason}")
        if item["risk_flags"]:
            lines.append(f"  风险 {'；'.join(item['risk_flags'][:2])}")

    portfolio_path = config.snapshot_path
    if portfolio_path.exists():
        lines.extend(["", "【持仓复盘】"])
        lines.extend(_render_portfolio_section(portfolio_path, items, stop_loss_pct=config.monitor.stop_loss_pct))
        # Check for stale trading triggers
        try:
            snapshot = load_portfolio_snapshot(portfolio_path)
            triggers = load_triggers(config.trading_plan_path)
            stale_warnings = check_stale_triggers(triggers, snapshot)
            if stale_warnings:
                lines.extend(["", "【⚠️ 过期触发单提醒】"])
                lines.extend(stale_warnings)
                lines.append("建议用 /plan 命令或直接编辑 trading-plan.json 更新触发区间。")
        except Exception:
            pass  # Non-critical — don't break the review for this

    # ── Tomorrow's action plan & orphan trigger check ──
    try:
        snapshot = load_portfolio_snapshot(portfolio_path)
        triggers = load_triggers(config.trading_plan_path)
        active_codes = {h.code for h in snapshot.holdings if h.quantity > 0}
        # Check for triggers on cleared positions
        orphan_triggers = [t for t in triggers if t.code not in active_codes]
        if orphan_triggers:
            lines.extend(["", "【⚠️ 已清仓触发单提醒】"])
            for t in orphan_triggers:
                lines.append(f"- {t.code} {t.name}：已清仓但触发单仍为 {t.action} {t.quantity}股 @ {t.price_min}-{t.price_max}，建议清理或改写为入场单")
        # Tomorrow's key levels
        lines.extend(["", "【明日操作计划】"])
        for holding in snapshot.holdings:
            if holding.quantity <= 0:
                continue
            item = item_map.get(holding.code)
            if not item:
                continue
            pnl = _pnl_pct(holding.cost_price, Decimal(str(item.get("current_price", holding.current_price))))
            current_price = Decimal(str(item.get("current_price", holding.current_price)))
            action = item.get("action", "hold")
            lines.append(f"- {holding.name}({holding.code})：持仓 {holding.quantity}股 浮盈亏 {_signed_decimal(pnl)}% | 建议 {action}")
            if pnl >= 10:
                lines.append(f"  🎯 止盈提醒：浮盈 {_signed_decimal(pnl)}%，已触发止盈关注区")
                # Show take profit tiers
                if config.monitor.take_profit_tiers:
                    for tier in config.monitor.take_profit_tiers:
                        if float(pnl) >= tier.profit_pct:
                            sell_qty = int(holding.quantity * Decimal(str(tier.sell_ratio)))
                            sell_qty = (sell_qty // 100) * 100 if sell_qty >= 100 else holding.quantity
                            lines.append(f"     {tier.label}：建议卖出 {sell_qty} 股（{tier.sell_ratio*100:.0f}%）")
            elif pnl >= 5:
                lines.append(f"  关注止盈：若冲高至 {_fmt_decimal(current_price * Decimal('1.05'))} 附近可考虑减仓")
            elif pnl <= -20:
                # Deep loss exit roadmap
                lines.append(f"  ⚠️ 深套退出路线图：浮亏 {_signed_decimal(pnl)}%")
                lines.append(f"  最近阻力位：MA15 约 {_fmt_decimal(current_price * Decimal('1.10')) if holding.cost_price > current_price * Decimal('2') else _fmt_decimal(holding.cost_price * Decimal('0.5'))}")
                lines.append(f"  第一阶段：反弹至 {_fmt_decimal(current_price * Decimal('1.15'))} 附近卖 50 股" if holding.quantity >= 100 else f"  反弹减仓：反弹至 {_fmt_decimal(current_price * Decimal('1.10'))} 附近减仓")
                lines.append(f"  保命底线：跌至 {_fmt_decimal(current_price * Decimal('0.90'))} 全部清仓" if holding.quantity > 100 else "  保命底线：跌至整数关口下方全部清仓")
                lines.append(f"  纪律：深套股绝不补仓，只等反弹减仓" if pnl <= -50 else "  纪律：不补仓，反弹减仓，止损保命")
            elif pnl <= -5:
                lines.append(f"  关注减亏：反弹至成本线 {_fmt_decimal(holding.cost_price)} 附近可减仓")
            lines.append(f"  止损参考：{_fmt_decimal(holding.cost_price * (1 - Decimal(str(config.monitor.stop_loss_pct)) / Decimal('100')))}")
        active_triggers = [t for t in triggers if t.code in active_codes]
        if active_triggers:
            lines.append("")
            lines.append("【明日触发单关注】")
            for t in active_triggers:
                lines.append(f"- {t.code} {t.name}：{t.action} {t.quantity}股，区间 {t.price_min}-{t.price_max}，回落 {t.fallback_price}")
        lines.append("")
        lines.append("以上为辅助参考，不构成投资建议。请根据明日盘前实际情况做出决策。")
        # Save structured plan record for next-day comparison
        _save_plan_record(config.review.data_dir, trade_date, snapshot, triggers, item_map, config)
    except Exception:
        pass  # Non-critical

    if trading_habit_profile is not None:
        lines.extend(["", "【交易习惯学习】"])
        lines.append(f"样本数: {trading_habit_profile.sample_count}")
        lines.append(f"画像: {trading_habit_profile.summary}")
        lines.append(
            f"建议已按习惯校准: 买入常用 {trading_habit_profile.preferred_buy_lot} 股 | "
            f"加仓常用 {trading_habit_profile.preferred_add_lot} 股 | "
            f"减仓习惯 {_fmt_decimal(trading_habit_profile.preferred_reduce_ratio * Decimal('100'))}%"
        )

    # ── Friday weekly wrap + cash deployment ──
    is_friday = trade_date.weekday() == 4
    if is_friday:
        lines.extend(["", "【周末准备 — 周线回顾】"])
        if portfolio_path.exists():
            try:
                snapshot = load_portfolio_snapshot(portfolio_path)
                for holding in snapshot.holdings:
                    if holding.quantity <= 0:
                        continue
                    pnl = _pnl_pct(holding.cost_price, holding.current_price)
                    lines.append(
                        f"- {holding.name}：周收盘 {_fmt_decimal(holding.current_price)}"
                        f" | 持仓盈亏 {_signed_decimal(pnl)}%"
                        f" | 距成本 {_fmt_decimal(abs(holding.current_price - holding.cost_price))}"
                    )
            except Exception:
                pass
        lines.append("- 周末关注：周末政策消息、外围市场走势、下周财经日历")
        lines.append("- 周一盘前简报将更新下周关键价位")

    # ── Cash deployment conditions ──
    if portfolio_path.exists():
        try:
            snapshot = load_portfolio_snapshot(portfolio_path)
            if snapshot.total_assets > 0:
                cash_pct = (snapshot.cash / snapshot.total_assets * 100)
                if cash_pct > 60:
                    lines.extend(["", "【现金部署条件】"])
                    lines.append(f"当前现金占比 {cash_pct:.0f}%，偏保守。")
                    lines.append("部署条件（需同时满足）：")
                    lines.append("  1. 大盘不暴跌（上证 > -1.5%）")
                    lines.append("  2. 标的评分 >= 84（买入阈值）")
                    lines.append("  3. 单票仓位不超过 35%")
                    lines.append("  4. 不补仓深套股（-30%+）")
                    lines.append("  审视现有持仓，优先加仓趋势向好、浮盈的标的")
        except Exception:
            pass

    lines.extend(["", "【结论】"])
    if avg_score is not None and avg_score >= Decimal("58"):
        lines.append("- 今日整体评分偏中性偏强，优先保留强势、弱势只做反弹处理。")
    elif avg_score is not None and avg_score <= Decimal("42"):
        lines.append("- 今日整体评分偏弱，控制仓位与现金比盲目抄底更重要。")
    else:
        lines.append("- 今日整体仍是分化市况，按个股评分和仓位纪律执行。")

    # ── LLM 明日解读 ──
    try:
        from .llm_analyst import generate_close_verdict
        llm_data: list[dict] = []
        if portfolio_path.exists():
            snap = load_portfolio_snapshot(portfolio_path)
            for h in snap.holdings:
                if h.quantity <= 0:
                    continue
                item = item_map.get(h.code, {})
                llm_data.append({
                    "name": h.name, "code": h.code, "quantity": h.quantity,
                    "cost_price": float(h.cost_price), "current_price": float(h.current_price),
                    "pnl_pct": float(_pnl_pct(h.cost_price, h.current_price)),
                    "action": item.get("action", "hold"),
                    "score": item.get("score"),
                })
        if llm_data:
            market_info = f"平均评分{avg_score:.0f}" if avg_score else ""
            verdict = generate_close_verdict(llm_data, avg_score=float(avg_score) if avg_score else 50, market_summary=market_info, today=trade_date.isoformat())
            if verdict:
                lines.append(f"\n【AI 明日解读】")
                lines.append(verdict)
    except Exception:
        pass

    lines.append(f"- 下次开盘：{next_session_str()}")
    lines.append("- 仅供参考，不构成投资建议。")
    return "\n".join(lines)


def _render_portfolio_section(snapshot_path: Path, items: list[dict], stop_loss_pct: float = 7.0) -> list[str]:
    snapshot = load_portfolio_snapshot(snapshot_path)
    item_map = {item["code"]: item for item in items}
    total_assets = snapshot.total_assets if snapshot.total_assets > 0 else Decimal("0")
    lines = [
        f"总资产: {_fmt_decimal(snapshot.total_assets)}",
        f"现金: {_fmt_decimal(snapshot.cash)}",
    ]
    stop_ratio = Decimal(str(stop_loss_pct)) / Decimal("100")
    for holding in snapshot.holdings:
        latest = item_map.get(holding.code)
        latest_price = Decimal(str(latest["current_price"])) if latest else holding.current_price
        pnl = _pnl_pct(holding.cost_price, latest_price)
        market_value = latest_price * Decimal(holding.quantity)
        weight = Decimal("0")
        if total_assets > 0:
            weight = (market_value / total_assets * Decimal("100")).quantize(Decimal("0.01"))
        action = latest["action"] if latest else "unknown"
        stop_line = ""
        if holding.cost_price > 0:
            stop_price = (holding.cost_price * (1 - stop_ratio)).quantize(Decimal("0.001"))
            dist = _pnl_pct(stop_price, latest_price)
            stop_line = f" | 止损参考 {_fmt_decimal(stop_price)}（距 {_signed_decimal(dist)}%）"
        cost_line = f" | 成本 {_fmt_decimal(holding.cost_price)} | 现价 {_fmt_decimal(latest_price)}" if holding.cost_price > 0 else ""
        lines.append(
            f"- {holding.name}({holding.code}) | 仓位 {_fmt_decimal(weight)}%{cost_line} | 浮盈亏 {_signed_decimal(pnl)}%{stop_line} | 最新动作 {action}"
        )
    return lines


def _save_review(data_dir: Path, trade_date: date, body: str) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{trade_date.isoformat()}-close-review.txt"
    path.write_text(body, encoding="utf-8")
    return path


def _plan_record_path(data_dir: Path, trade_date: date) -> Path:
    return data_dir / "portfolio" / f"plan-record-{trade_date.isoformat()}.json"


def _save_plan_record(data_dir: Path, trade_date: date, snapshot, triggers, item_map, config) -> None:
    """Save structured plan record for next-day comparison in pre-market briefing."""
    from .trading_plan import TriggerInstruction

    record_dir = data_dir / "portfolio"
    record_dir.mkdir(parents=True, exist_ok=True)
    holdings_data = []
    for holding in snapshot.holdings:
        if holding.quantity <= 0:
            continue
        item = item_map.get(holding.code, {})
        current_price = Decimal(str(item.get("current_price", holding.current_price)))
        pnl = _pnl_pct(holding.cost_price, current_price)
        holdings_data.append({
            "code": holding.code,
            "name": holding.name,
            "quantity": holding.quantity,
            "cost_price": float(holding.cost_price),
            "current_price": float(current_price),
            "pnl_pct": float(pnl),
            "planned_action": item.get("action", "hold"),
            "planned_score": item.get("score"),
        })
    active_codes = {h["code"] for h in holdings_data}
    triggers_data = []
    for t in triggers:
        if isinstance(t, TriggerInstruction):
            triggers_data.append({
                "code": t.code,
                "name": getattr(t, "name", ""),
                "action": t.action,
                "quantity": t.quantity,
                "price_min": float(t.price_min) if isinstance(t.price_min, Decimal) else t.price_min,
                "price_max": float(t.price_max) if isinstance(t.price_max, Decimal) else t.price_max,
                "fallback_price": float(t.fallback_price) if isinstance(t.fallback_price, Decimal) else t.fallback_price,
                "is_orphan": t.code not in active_codes,
            })
        else:
            code = getattr(t, "code", "unknown")
            triggers_data.append({
                "code": code,
                "name": getattr(t, "name", ""),
                "action": getattr(t, "action", ""),
                "quantity": getattr(t, "quantity", 0),
                "price_min": float(getattr(t, "price_min", 0)),
                "price_max": float(getattr(t, "price_max", 0)),
                "fallback_price": float(getattr(t, "fallback_price", 0)),
                "is_orphan": code not in active_codes,
            })
    record = {
        "plan_date": trade_date.isoformat(),
        "generated_at": datetime.now(MARKET_TZ).isoformat(timespec="seconds"),
        "holdings": holdings_data,
        "triggers": triggers_data,
    }
    path = _plan_record_path(data_dir, trade_date)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def load_plan_record(data_dir: Path, trade_date: date) -> dict | None:
    """Load plan record for a given trade date. Returns None if not found."""
    path = _plan_record_path(data_dir, trade_date)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load plan record error=%s", exc)
        return None


def find_latest_plan_record(data_dir: Path) -> dict | None:
    """Find the most recent plan record file."""
    record_dir = data_dir / "portfolio"
    if not record_dir.exists():
        return None
    files = sorted(record_dir.glob("plan-record-*.json"), reverse=True)
    for f in files:
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to load plan record %s error=%s", f.name, exc)
    return None


def _review_state_path(data_dir: Path) -> Path:
    return data_dir / "close-review-state.json"


def _load_review_state(data_dir: Path) -> dict:
    path = _review_state_path(data_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to load review state error=%s", exc)
        return {}


def _avg(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    total = sum(values, Decimal("0"))
    return (total / Decimal(len(values))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _fmt_optional(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.0f}"


def _fmt_float(value: float) -> str:
    return f"{value:.3f}"


def _fmt_decimal(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _signed(value: float) -> str:
    return f"+{value:.2f}" if value > 0 else f"{value:.2f}"


def _signed_decimal(value: Decimal) -> str:
    scaled = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"+{scaled}" if scaled > 0 else str(scaled)


def _pnl_pct(cost_price: Decimal, current_price: Decimal) -> Decimal:
    if cost_price <= 0:
        return Decimal("0")
    return (((current_price - cost_price) / cost_price) * Decimal("100")).quantize(Decimal("0.01"))
