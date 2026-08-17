from __future__ import annotations
import logging


import json
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import AppConfig
from .market_hours import MARKET_TZ, next_session_str
from .models import build_position_exit_plan
from .portfolio import load_snapshot as load_portfolio_snapshot
from .storage import connect_db, fetch_daily_review_snapshot, fetch_latest_trade_date
from .trading_plan import check_stale_triggers, load_triggers, remove_orphan_triggers, save_triggers
from .logging_utils import get_logger

logger = logging.getLogger(__name__)
from .signal_tracker import evaluate_signal_accuracy, format_accuracy_report
from .feedback_loop import run_daily_feedback
from .trader_feedback import run_trader_feedback
from .opportunity_scanner import (
    scan as scan_opportunities,
    suggest_position,
    build_exit_plan,
    build_trade_ideas,
    can_afford_candidate,
)
from .opportunity_journal import record_daily_opportunities, render_recent_opportunity_validation

logger = get_logger(__name__)

_QUALITY_CACHE: dict | None = None


def _load_signal_quality() -> dict:
    """每票历史信号质量 {code6: {buy: {n, ret5, win5}, ...}}，供【明日机会】标注。
    数据由 scripts/backtest_daily_tencent.py --quality-json 生成。"""
    global _QUALITY_CACHE
    if _QUALITY_CACHE is not None:
        return _QUALITY_CACHE
    try:
        p = Path(__file__).resolve().parent.parent / "data" / "signal_quality.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        _QUALITY_CACHE = {k[-6:]: v for k, v in data.items()}
    except Exception:  # noqa: BLE001 —— 无质量数据时标注降级为空
        _QUALITY_CACHE = {}
    return _QUALITY_CACHE


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
    # ── 市场宽度（在整体评估前获取，让评估参考真实市场状态） ──
    market_context = _safe_market_context(config)
    body = _render_review_body(config, trade_date, items, requested_trade_date=requested_trade_date, market_context=market_context)

    # ── 资金面 + 市场温度 ──
    try:
        from .fund_flow import get_northbound_flow, get_stock_financials
        from .market_breadth import format_breadth_md
        from .portfolio import load_snapshot as _load_snap
        snap = _load_snap(config.snapshot_path)
        codes = [h.code for h in snap.holdings]
        nb = get_northbound_flow(force_refresh=True)
        if not nb.get("error"):
            hk_total = nb["hk_total_net_yi"]
            direction = "🟢净流入" if hk_total > 0 else ("🔴净流出" if hk_total < 0 else "⚪持平")
            body += f"\n\n💰 **北向资金**: {direction} {abs(hk_total):.1f}亿（沪{nb['hk2sh']['net_inflow_yi']:+.1f}/深{nb['hk2sz']['net_inflow_yi']:+.1f}）"
        symbols = ["sh" + c if c.startswith(("6", "9")) else "sz" + c for c in codes]
        fin = get_stock_financials(symbols)
        if fin:
            body += "\n📊 **持仓估值**:"
            for code in codes:
                f = fin.get(code)
                if f:
                    pe_s = f"{f['pe']:.0f}" if f["pe"] > 0 else "亏损"
                    body += f"\n  {code} PE{pe_s} PB{f['pb']:.1f} 市值{f['market_cap_yi']:.0f}亿 量比{f['volume_ratio']:.2f}"
        breadth = format_breadth_md(codes)
        if breadth.strip():
            body += f"\n\n{breadth}"
    except Exception as exc:
        logger.warning("stock_advisor/review.py:build_close_review failed: %s", exc)

    # ── Signal accuracy tracking ──
    try:
        accuracy_stats = evaluate_signal_accuracy(days_lookback=3)
        accuracy_report = format_accuracy_report(accuracy_stats)
        body += f"\n\n{accuracy_report}"
    except Exception as exc:
        logger.warning("stock_advisor/review.py:build_close_review failed: %s", exc)
        pass  # Non-critical — don't break the review for signal tracking

    # ── Feedback loop: evaluate debate accuracy + update agent weights ──
    try:
        actual_prices = {item["code"]: Decimal(str(item["current_price"])) for item in items if item.get("current_price")}
        feedbacks = run_daily_feedback(actual_prices, today=trade_date)
        if feedbacks:
            correct = sum(1 for f in feedbacks if f.was_correct)
            body += f"\n\n🔄 **Agent反馈回路**: 今日辩论{len(feedbacks)}条，命中{correct}条（{correct/len(feedbacks)*100:.0f}%），权重已自动更新。"
        else:
            body += "\n\n🔄 **Agent反馈回路**: 今日无辩论记录可供验证。"
    except Exception as exc:
        logger.warning("stock_advisor/review.py:build_close_review failed: %s", exc)
        pass  # Non-critical

    # ── Trader feedback: compare user's real trades against system signals ──
    try:
        trader_report = run_trader_feedback()
        if trader_report:
            body += f"\n\n{trader_report}"
    except Exception as exc:
        logger.warning("stock_advisor/review.py:build_close_review failed: %s", exc)
        pass  # Non-critical

    # ── Threshold optimization: grid-search against historical signals ──
    try:
        from .threshold_optimizer import grid_search, format_report, load_signals, auto_apply_if_better
        signals = load_signals()
        if signals:
            results = grid_search(signals)
            opt_report = format_report(
                results,
                config.monitor.decision_thresholds.buy_score,
                config.monitor.decision_thresholds.hold_score,
            )
            body += f"\n\n{opt_report}"
            # Auto-apply if significantly better
            applied = auto_apply_if_better("config.yaml", min_gap_pct=5.0)
            if applied:
                body += f"\n\n⚡ **阈值已自动更新**: buy={applied['old_buy']}→{applied['new_buy']} hold={applied['old_hold']}→{applied['new_hold']}（命中率+{applied['gap_pct']:.1f}%）"
    except Exception as exc:
        logger.warning("stock_advisor/review.py:build_close_review failed: %s", exc)
        pass  # Non-critical

    # ── v1.55.4: Debate→Trigger sync ──
    # 收盘辩论结论反哺trading_plan.json，替换"等反弹"为实际行动
    try:
        from .debate_trigger_sync import sync_debate_to_triggers
        sync_actions = sync_debate_to_triggers(config.review.data_dir)
        if sync_actions:
            body += "\n\n🎯 **辩论→触发单同步**:"
            for act in sync_actions:
                body += f"\n  {act}"
    except Exception as exc:
        logger.warning("stock_advisor/review.py:build_close_review failed: %s", exc)
        pass  # Non-critical

    # ── Auto sell-plan & profit trigger sync through unified state model ──
    # 浮盈票自动创建止盈触发；弱势票自动创建计划卖点。
    # 不再绕道旧版 sync_profit_triggers / sync_exit_plan_triggers 直写文件。
    try:
        from .state_builder import generate_and_sync_triggers
        snap_path = config.snapshot_path
        trigger_path = config.trading_plan.path
        if snap_path.exists() and trigger_path.exists():
            created_profit, created_exit = generate_and_sync_triggers(
                snap_path,
                trigger_path,
                exit_max_single_position_pct=config.monitor.risk_controls.max_single_position_pct,
            )
            if created_profit > 0:
                body += f"\n\n💰 **止盈触发同步**: 为{created_profit}只浮盈持仓自动创建止盈触发单"
            if created_exit > 0:
                body += f"\n\n🎯 **卖点计划同步**: 为{created_exit}只弱势持仓自动创建计划卖点触发单"
    except Exception as exc:
        logger.warning("stock_advisor/review.py:build_close_review failed: %s", exc)
        pass  # Non-critical

    # ── v1.55.9: Attach proactive opportunity scan to close review ──
    # 收盘后直接给出明日可看的新机会，不只盯现有持仓扛单。
    try:
        exclude_codes: set[str] = set()
        if config.snapshot_path.exists():
            snapshot = load_portfolio_snapshot(config.snapshot_path)
            exclude_codes = {h.code for h in snapshot.holdings if h.quantity > 0}
        candidates = scan_opportunities(
            config_path="config.yaml",
            top_n=3,
            exclude_codes=exclude_codes,
            max_change_pct=5.0,
        )
        if candidates:
            record_daily_opportunities(config.review.data_dir, trade_date, candidates, config)
            candidates = [c for c in candidates if can_afford_candidate(config, c.current_price, c.composite_score)]
        if candidates:
            body += "\n\n🔎 **明日主动机会**:"
            for idx, cand in enumerate(candidates, 1):
                stop_price = (cand.current_price * Decimal("0.93")).quantize(Decimal("0.01"))
                position = suggest_position(config, cand.current_price, cand.composite_score)
                if position.quantity > 0:
                    position_text = f"{position.label}{position.quantity}股"
                else:
                    position_text = "现金不足，先观察"
                top_flags = " / ".join(flag.lstrip("🟢🟡🔴🟠📏💪🔔📊📈🔥⚠️ ") for flag in cand.flags[:2])
                line = (
                    f"\n{idx}. {cand.name}({cand.code}) {cand.current_price}"
                    f" | 分{cand.composite_score} | {position_text} | 止损{stop_price}"
                )
                if top_flags:
                    line += f" | {top_flags}"
                body += line
    except Exception as exc:
        logger.warning("stock_advisor/review.py:build_close_review failed: %s", exc)
        pass  # Non-critical

    # ── v1.55.17: Validate proactive opportunities against next-day/day-3 outcomes ──
    try:
        body += "\n\n" + render_recent_opportunity_validation(conn, config.review.data_dir, as_of_date=trade_date)
    except Exception as exc:
        logger.warning("stock_advisor/review.py:build_close_review failed: %s", exc)
        pass  # Non-critical

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
    try:
        state = _load_review_state(config.review.data_dir)
        return state.get("last_sent_trade_date") == trade_date.isoformat()
    except (json.JSONDecodeError, FileNotFoundError, PermissionError) as exc:
        logger.warning("Close review state file corrupted/missing: %s — treating as unsent", exc)
        return False


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


def _safe_market_context(config: AppConfig) -> dict | None:
    """Safely fetch market breadth context for overall assessment.
    Returns dict with up/down stats or None on failure.
    """
    try:
        from .market_breadth import get_market_snapshot
        snap = get_market_snapshot()
        breadth_up = snap.get("breadth_up", 0)
        breadth_down = snap.get("breadth_down", 0)
        total_stocks = max(breadth_up + breadth_down, 1)
        return {
            "up_pct": round(breadth_up / total_stocks * 100, 1),
            "up_count": breadth_up,
            "down_count": breadth_down,
            "sector_up": snap.get("up_sectors", 0),
            "sector_total": max(snap.get("sector_count", 1), 1),
            "temperature": snap.get("temperature", "无数据"),
        }
    except Exception as exc:
        logger.warning("stock_advisor/review.py:_safe_market_context failed: %s", exc)
        return None


def _render_review_body(config: AppConfig, trade_date: date, items: list[dict], *,
                        requested_trade_date: date,
                        market_context: dict | None = None) -> str:
    lines = [f"【收盘复盘】{trade_date.isoformat()}"]
    if trade_date != requested_trade_date:
        lines.append(f"说明: 当日暂无落库行情，已回退到最近交易日 {trade_date.isoformat()}")
    if not items:
        lines.append("今日暂无落库行情数据，未生成复盘明细。")
        lines.append("建议确认 monitor-daemon 是否正常运行。")
        return "\n".join(lines)

    scores = [Decimal(str(item["score"])) for item in items if item["score"] is not None]
    avg_score = _avg(scores)
    reduce_like = [item["code"] for item in items if item["action"] in {"reduce", "avoid"}]

    # ── 整体一句话 ──
    summary_parts = []
    if avg_score is not None:
        if avg_score >= 58:
            summary_parts.append("偏强")
        elif avg_score <= 42:
            summary_parts.append("偏弱")
        else:
            summary_parts.append("分化")
    summary_parts.append(f"{len(items)}只标的")
    # ── 叠加市场宽度 ──
    if market_context and market_context.get("temperature"):
        temp = market_context["temperature"]
        up_pct = market_context["up_pct"]
        sector_up = market_context["sector_up"]
        sector_total = market_context["sector_total"]
        summary_parts.append(f"市场{temp}（↑{up_pct}% {sector_up}/{sector_total}板块）")
    elif market_context:
        summary_parts.append(f"涨跌比↑{market_context['up_pct']}%")
    lines.append("整体：" + " | ".join(summary_parts))

    if reduce_like:
        lines.append(f"⚠️ 优先减仓：{', '.join(reduce_like)}")

    lines.append("")
    lines.append("【明日优先动作】")
    if reduce_like:
        lines.append(f"1. 先处理弱势持仓：{', '.join(reduce_like)}")
    else:
        lines.append("1. 明天先不减仓，观察强弱分化")
    lines.append("2. 新仓只做计划内买点，不追高")
    lines.append("3. 所有持仓先看卖点，再看是否继续持有")

    # ── 【标的复盘】每只 1-2 行 ──
    lines.append("")
    action_emoji = {"buy": "🟢", "hold": "🟡", "reduce": "🔴", "avoid": "⛔"}
    for item in items:
        emoji = action_emoji.get(item["action"], "⚪")
        # 主线：名称 + 涨跌 + 动作 + 一句话建议
        header = f"{emoji} {item['code']} {item['name']} | {_signed(item['change_percent'])}% | **{item['action']}**"
        if item["trade_advice"]:
            header += f" | {_shorten_advice(item['trade_advice'], 30)}"
        if item.get("score") is not None:
            header += f" | 分{item['score']:.0f}"
        lines.append(header)

        # 副线：关键理由（只取第一条）
        if item["rationale"]:
            reason = item["rationale"][0]
            if len(reason) > 60:
                reason = reason[:57] + "..."
            lines.append(f"  {reason}")

        # 关键风险 tag
        if item["risk_flags"]:
            important_flags = [f for f in item["risk_flags"][:3] if f not in ("📊", "📉")]
            if important_flags:
                lines.append(f"  ⚠️ {' | '.join(important_flags)}")

    portfolio_path = config.snapshot_path
    if portfolio_path.exists():
        lines.extend(["", "【持仓复盘】"])
        lines.extend(_render_portfolio_section(portfolio_path, items, stop_loss_pct=config.monitor.stop_loss_pct))
        try:
            snapshot_for_exit = load_portfolio_snapshot(portfolio_path)
            exit_lines = []
            for holding in snapshot_for_exit.holdings:
                if holding.quantity <= 0:
                    continue
                plan = build_position_exit_plan(holding)
                exit_lines.append(
                    f"- {holding.name}：止损 {plan.stop_loss}；先看 {plan.first_take_profit}；强势再看 {plan.final_take_profit}；移动止盈 {plan.trailing_take_profit_pct}% 。{plan.note}"
                )
            if exit_lines:
                lines.extend(["", "【卖点计划】"])
                lines.extend(exit_lines)
        except Exception as exc:
            logger.warning("stock_advisor/review.py:_render_review_body failed: %s", exc)
        # Check for stale trading triggers
        try:
            snapshot = load_portfolio_snapshot(portfolio_path)
            triggers = load_triggers(config.trading_plan.path)
            stale_warnings = check_stale_triggers(triggers, snapshot)
            if stale_warnings:
                lines.extend(["", "【⚠️ 过期触发单提醒】"])
                lines.extend(stale_warnings)
                lines.append("建议用 /plan 命令或直接编辑 trading-plan.json 更新触发区间。")
        except Exception as exc:
            logger.warning("stock_advisor/review.py:_render_review_body failed: %s", exc)
            pass  # Non-critical — don't break the review for this

    # ── Tomorrow's action plan & orphan trigger check ──
    try:
        item_map = {item["code"]: item for item in items}
        snapshot = load_portfolio_snapshot(portfolio_path)
        triggers = load_triggers(config.trading_plan.path)
        active_codes = {h.code for h in snapshot.holdings if h.quantity > 0}
        # Auto-clean triggers on cleared positions
        orphan_triggers = [t for t in triggers.values() if t.code not in active_codes]
        if orphan_triggers:
            cleaned, removed = remove_orphan_triggers(triggers, active_codes)
            if removed > 0 and config.trading_plan.path:
                save_triggers(config.trading_plan.path, cleaned)
                lines.extend(["", "【🧹 已自动清理过期触发单】"])
                for t in orphan_triggers:
                    lines.append(f"- ✅ 已移除 {t.code} {t.name}（已清仓，触发单 {t.action} {t.quantity}股 @ {t.price_min}-{t.price_max}）")
        # ── v1.57.0: 明日机会 (非持仓 buy/高分票) — 上班族不盯盘, 收盘统一给入场价 ──
        try:
            opp_candidates = [
                it for it in items
                if it["code"] not in active_codes
                and it.get("score") is not None and float(it["score"]) >= 80
            ]
            if opp_candidates:
                lines.extend(["", "【明日机会】"])
                qmap = _load_signal_quality()
                for it in opp_candidates[:5]:
                    price = Decimal(str(it.get("current_price") or 0))
                    if price <= 0:
                        continue
                    entry_lo = (price * Decimal("0.985")).quantize(Decimal("0.01"))
                    entry_hi = (price * Decimal("0.995")).quantize(Decimal("0.01"))
                    abort = (price * Decimal("0.97")).quantize(Decimal("0.01"))
                    sc = float(it["score"])
                    tier = "确信仓" if sc >= 92 else ("标准仓" if sc >= 86 else "试探仓")
                    reason = (it.get("rationale") or [""])[0]
                    if len(reason) > 40:
                        reason = reason[:37] + "..."
                    lines.append(
                        f"- 🟢 {it['name']}({it['code']}) 今日{_signed(it['change_percent'])}% 分{sc:.0f} {tier}"
                    )
                    lines.append(
                        f"  入场: 回踩 {entry_lo}-{entry_hi} 挂单 | 放弃: 高开>3% 或 破 {abort} | {reason}"
                    )
                    # v1.57.1: 历史信号质量标注 (backtest 回放 120 日)
                    bq = qmap.get(it["code"], {}).get("buy")
                    if bq and bq.get("n", 0) > 0:
                        tag = f"📊 历史buy信号{bq['n']}次 5日{bq['ret5']:+.2f}% 胜率{bq['win5']}%"
                        if bq["ret5"] <= 0:
                            tag = f"⚠️ {tag}（该票buy信号历史负期望，谨慎）"
                        lines.append(f"  {tag}")
        except Exception as exc:
            logger.warning("review.py 明日机会 section failed: %s", exc)

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
            exit_plan = build_position_exit_plan(holding)
            lines.append(f"- {holding.name}({holding.code})：持仓 {holding.quantity}股 浮盈亏 {_signed_decimal(pnl)}% | 建议 {action}")
            lines.append(f"  卖点1：{_fmt_decimal(exit_plan.first_take_profit)}（先卖 1/3，优先兑现一部分）")
            lines.append(f"  卖点2：{_fmt_decimal(exit_plan.final_take_profit)}（卖剩余仓位的一半，强势延续才看）")
            lines.append(f"  止损：{_fmt_decimal(exit_plan.stop_loss)}（跌破直接执行）")
            lines.append(f"  纪律：{exit_plan.note}")
        active_triggers = [t for t in triggers.values() if t.code in active_codes]
        if active_triggers:
            lines.append("")
            lines.append("【明日触发单关注】")
            for t in active_triggers:
                lines.append(f"- {t.code} {t.name}：{t.action} {t.quantity}股，区间 {t.price_min}-{t.price_max}，回落 {t.fallback_price}")
        lines.append("")
        lines.append("以上为辅助参考，不构成投资建议。请根据明日盘前实际情况做出决策。")
        # Save structured plan record for next-day comparison
        _save_plan_record(config.review.data_dir, trade_date, snapshot, triggers, item_map, config)
    except Exception as exc:
        logger.warning("stock_advisor/review.py:_render_review_body failed: %s", exc)
        pass  # Non-critical

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
            except Exception as exc:
                logger.warning("stock_advisor/review.py:_render_review_body failed: %s", exc)
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
        except Exception as exc:
            logger.warning("stock_advisor/review.py:_render_review_body failed: %s", exc)

    lines.extend(["", "【结论】"])
    if avg_score is not None and avg_score >= Decimal("58"):
        lines.append("- 今日整体评分偏中性偏强，优先保留强势、弱势只做反弹处理。")
    elif avg_score is not None and avg_score <= Decimal("42"):
        lines.append("- 今日整体评分偏弱，控制仓位与现金比盲目抄底更重要。")
    else:
        lines.append("- 今日整体仍是分化市况，按个股评分和仓位纪律执行。")

    # ── 明日计划总结（结构化，不依赖 LLM 自由生成） ──
    try:
        if portfolio_path.exists():
            snap = load_portfolio_snapshot(portfolio_path)
            plan_lines: list[str] = []
            for h in snap.holdings:
                if h.quantity <= 0:
                    continue
                item = item_map.get(h.code, {})
                pnl = _pnl_pct(h.cost_price, h.current_price)
                action = item.get("action", "hold")
                exit_plan = build_position_exit_plan(h)
                if action in ("reduce", "sell", "avoid"):
                    plan_lines.append(
                        f"- {h.name}：{action}，卖点{_fmt_decimal(exit_plan.first_take_profit)}先卖1/3，止损{_fmt_decimal(exit_plan.stop_loss)}"
                    )
                elif action == "buy":
                    plan_lines.append(f"- {h.name}：{action}，按买点入场，止损到位执行")
                else:
                    plan_lines.append(f"- {h.name}：持有观察")
            if plan_lines:
                lines.append("")
                lines.append("【明日执行计划】")
                lines.extend(plan_lines)

            # ── 集中度风险应对 ──
            conc_risks = []
            for h in snap.holdings:
                if h.quantity <= 0 or snap.total_assets <= 0:
                    continue
                mv = h.current_price * Decimal(h.quantity)
                conc = float(mv / snap.total_assets)
                if conc > 0.50:
                    exit_plan = build_position_exit_plan(h)
                    conc_risks.append(
                        f"🚨 {h.name}占{conc*100:.0f}%仓位（上限50%）——"
                        f"周一优先卖点1={_fmt_decimal(exit_plan.first_take_profit)}先降1/3，"
                        f"再挂卖点2={_fmt_decimal(exit_plan.final_take_profit)}再降半仓，目标仓位<50%"
                    )
            if conc_risks:
                lines.append("")
                lines.append("【集中度风险应对】")
                lines.extend(conc_risks)

    except Exception as exc:
        logger.warning("stock_advisor/review.py:_render_review_body failed: %s", exc)

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
    from .trading_plan import TradeTrigger

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
    for t in triggers.values():
        if isinstance(t, TradeTrigger):
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


def _shorten_advice(advice: str, max_len: int = 30) -> str:
    """Trim verbose trade advice to a compact form."""
    advice = advice.strip()
    if len(advice) <= max_len:
        return advice
    return advice[:max_len-1] + "…"


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
