from __future__ import annotations

import argparse

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .config import load_config, require_valid_config, validate_config
from .snapshot_parser import parse_portfolio_text
from .habit_learning import build_trading_habit_profile, render_trading_habit_profile
from .backtest import (
    optimize_decision_thresholds,
    render_minute_backtest,
    render_optimization_report,
    run_daily_backtest,
    run_enhanced_daily_backtest,
    run_minute_backtest,
)
from .cash_deploy import generate_deploy_signal, render_deploy_signal
from .data_review import generate_data_review
from .trade_journal import TradeJournal
from .briefing import format_mobile_digest, format_mobile_replay, format_mobile_signal
from .codex_bridge import pull_codex_notifications
from .feishu_bot_server import serve_feishu_bot
from .market_overview import build_market_overview, render_market_overview
from .historical import (
    analyze_historical_point,
    compare_historical_points,
    render_historical_advice,
    render_historical_compare,
)
from .models import StockRef, TradeFillRecord
from .notify import deliver_feishu_message, flush_failed_notifications, notify_feishu_if_enabled
from .portfolio import build_daily_report, compute_cash_ratio, compute_position_ratio, find_holding, generate_portfolio_report, load_previous_snapshot, load_snapshot, save_snapshot
from .logging_utils import get_logger
from .market_hours import is_high_volatility_period, next_session_str
from .providers import EastmoneyMarketSnapshotProvider, EastmoneyMinuteHistoryProvider, TencentQuoteProvider
from .analysis import analyze_quotes
from .review import build_close_review
from .trading_plan import load_triggers
from .runtime import MonitorRuntime
from .storage import (
    cache_quotes,
    connect_db,
    fetch_latest_briefing,
    insert_trade_fill,
    load_recent_quotes,
    persist_observation,
    prune_old_data,
    replay_signal_stats,
)
from .trading_plan import (
    apply_trade_fill,
    build_post_fill_execution_sheet,
    ensure_trigger_file,
    load_snapshot as load_trade_snapshot,
    save_snapshot as save_trade_snapshot,
)

from .shared_helpers import build_provider, load_market_context, load_stock_history, parse_history_datetime


logger = get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(prog="stock-advisor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    monitor_parser = subparsers.add_parser("monitor-once", help="单次获取行情并输出观察报告")
    monitor_parser.add_argument("--config", required=True, help="配置文件路径")
    monitor_parser.add_argument("--notify", action="store_true", help="强制发送 webhook")
    monitor_parser.add_argument("--mobile", action="store_true", help="输出手机友好摘要")

    daemon_parser = subparsers.add_parser("monitor-daemon", help="常驻轮询行情并按间隔执行")
    daemon_parser.add_argument("--config", required=True, help="配置文件路径")

    portfolio_parser = subparsers.add_parser("portfolio-report", help="生成收盘持仓建议")
    portfolio_parser.add_argument("--config", required=True, help="配置文件路径")
    portfolio_parser.add_argument("--snapshot", required=True, help="持仓快照 JSON 文件")
    portfolio_parser.add_argument("--notify", action="store_true", help="发送 webhook")

    replay_parser = subparsers.add_parser("replay-signals", help="回放历史信号并统计后续表现")
    replay_parser.add_argument("--config", required=True, help="配置文件路径")
    replay_parser.add_argument("--symbol", help="按 symbol 过滤，如 sh601698")
    replay_parser.add_argument("--level", help="按信号级别过滤，如 ALERT/INFO/NEUTRAL")
    replay_parser.add_argument("--action", help="按动作过滤，如 avoid/reduce/hold")
    replay_parser.add_argument("--notify", action="store_true", help="把回放摘要发送到飞书")

    digest_parser = subparsers.add_parser("mobile-brief", help="输出适合手机飞书机器人的简报")
    digest_parser.add_argument("--config", required=True, help="配置文件路径")
    digest_parser.add_argument("--notify", action="store_true", help="把简报发送到飞书")

    market_parser = subparsers.add_parser("market-scan", help="输出全市场扫描与热点板块概览")
    market_parser.add_argument("--config", required=True, help="配置文件路径")
    market_parser.add_argument("--mobile", action="store_true", help="输出手机友好摘要")
    market_parser.add_argument("--notify", action="store_true", help="把市场概览发送到飞书")

    bot_parser = subparsers.add_parser("serve-feishu-bot", help="启动飞书机器人命令回调服务")
    bot_parser.add_argument("--config", required=True, help="配置文件路径")

    fill_parser = subparsers.add_parser("record-fill", help="记录成交结果并更新本地持仓快照")
    fill_parser.add_argument("--snapshot", required=True, help="持仓快照 JSON 文件")
    fill_parser.add_argument("--config", help="配置文件路径，用于记录成交历史并更新习惯画像")
    fill_parser.add_argument("--side", required=True, choices=["buy", "sell"], help="成交方向")
    fill_parser.add_argument("--code", required=True, help="股票代码")
    fill_parser.add_argument("--quantity", required=True, type=int, help="成交数量")
    fill_parser.add_argument("--price", required=True, help="成交价")

    init_trade_plan_parser = subparsers.add_parser("init-trading-plan", help="生成默认交易计划文件")
    init_trade_plan_parser.add_argument("--config", required=True, help="配置文件路径")

    validate_parser = subparsers.add_parser("validate-config", help="校验配置文件和交易计划")
    validate_parser.add_argument("--config", required=True, help="配置文件路径")

    flush_parser = subparsers.add_parser("flush-failed-notifications", help="重放失败的 webhook 通知")
    flush_parser.add_argument("--config", required=False, help="保留参数位，兼容统一运维脚本")

    codex_pull_parser = subparsers.add_parser("pull-codex-notifications", help="读取本地 Codex bridge 推送队列")
    codex_pull_parser.add_argument("--limit", type=int, default=20, help="最多读取多少条未读消息，默认 20")
    codex_pull_parser.add_argument("--keep-unread", action="store_true", help="只读取，不标记已读")

    review_parser = subparsers.add_parser("close-review", help="生成收盘复盘报告")
    review_parser.add_argument("--config", required=True, help="配置文件路径")
    review_parser.add_argument("--notify", action="store_true", help="把收盘复盘发送到飞书")

    advice_at_parser = subparsers.add_parser("advice-at", help="按任意历史时点重算分钟级建议")
    advice_at_parser.add_argument("--config", required=True, help="配置文件路径")
    advice_at_parser.add_argument("--at", required=True, help="历史时点，如 2026-04-17 14:20 或 2026-04-17T14:20:00")
    advice_at_parser.add_argument("--symbol", action="append", help="指定股票代码或 symbol，可重复传入")
    advice_at_parser.add_argument("--mobile", action="store_true", help="输出手机友好摘要")
    advice_at_parser.add_argument("--notify", action="store_true", help="把历史时点建议发送到飞书")

    compare_at_parser = subparsers.add_parser("compare-at", help="比较两个历史时点的建议变化")
    compare_at_parser.add_argument("--config", required=True, help="配置文件路径")
    compare_at_parser.add_argument("--from-time", required=True, help="起始时点，如 2026-04-17 14:20")
    compare_at_parser.add_argument("--to-time", required=True, help="结束时点，如 2026-04-17 15:00")
    compare_at_parser.add_argument("--symbol", action="append", help="指定股票代码或 symbol，可重复传入")
    compare_at_parser.add_argument("--mobile", action="store_true", help="输出手机友好摘要")
    compare_at_parser.add_argument("--notify", action="store_true", help="把时点对比发送到飞书")

    backtest_parser = subparsers.add_parser("backtest-minutes", help="回测最近几日分钟级信号表现")
    backtest_parser.add_argument("--config", required=True, help="配置文件路径")
    backtest_parser.add_argument("--days", type=int, default=5, help="回测最近几日，默认 5")
    backtest_parser.add_argument("--symbol", action="append", help="指定股票代码或 symbol，可重复传入")
    backtest_parser.add_argument("--mobile", action="store_true", help="输出手机友好摘要")
    backtest_parser.add_argument("--notify", action="store_true", help="把分钟回测摘要发送到飞书")

    optimize_parser = subparsers.add_parser("optimize-thresholds", help="基于分钟回测结果给出更优动作阈值")
    optimize_parser.add_argument("--config", required=True, help="配置文件路径")
    optimize_parser.add_argument("--days", type=int, default=5, help="回看最近几日，默认 5")
    optimize_parser.add_argument("--symbol", action="append", help="指定股票代码或 symbol，可重复传入")
    optimize_parser.add_argument("--mobile", action="store_true", help="输出手机友好摘要")
    optimize_parser.add_argument("--notify", action="store_true", help="把阈值优化摘要发送到飞书")
    optimize_parser.add_argument("--apply", action="store_true", help="自动将最优阈值写入配置文件（仅在建议更换时生效）")

    habit_parser = subparsers.add_parser("habit-profile", help="查看系统学习到的交易习惯画像")
    habit_parser.add_argument("--config", required=True, help="配置文件路径")
    habit_parser.add_argument("--mobile", action="store_true", help="输出手机友好摘要")

    prune_parser = subparsers.add_parser("prune-data", help="清理数据库中的过期数据")
    prune_parser.add_argument("--config", required=True, help="配置文件路径")
    prune_parser.add_argument("--retention-days", type=int, default=90, help="保留天数（默认 90）")

    status_parser = subparsers.add_parser("status", help="输出当前系统状态：交易日历、持仓摘要、触发单")
    status_parser.add_argument("--config", required=True, help="配置文件路径")

    # ── 现金部署信号 ──
    deploy_parser = subparsers.add_parser("cash-deploy", help="检查现金部署条件，输出可入场标的和买入建议")
    deploy_parser.add_argument("--config", required=True, help="配置文件路径")
    deploy_parser.add_argument("--mobile", action="store_true", help="输出手机友好摘要")
    deploy_parser.add_argument("--notify", action="store_true", help="把部署信号发送到飞书")

    # ── 日线回测 ──
    daily_bt_parser = subparsers.add_parser("backtest-daily", help="单只股票日线级别回测，验证买入次日胜率")
    daily_bt_parser.add_argument("--config", required=True, help="配置文件路径")
    daily_bt_parser.add_argument("--stock", required=True, help="股票代码，如 601698")
    daily_bt_parser.add_argument("--days", type=int, default=60, help="回测天数（默认60）")
    daily_bt_parser.add_argument("--mobile", action="store_true", help="输出手机友好摘要")
    daily_bt_parser.add_argument("--notify", action="store_true", help="把回测结果发送到飞书")

    enhanced_bt_parser = subparsers.add_parser("backtest-enhanced", help="增强回测：多窗口+方向准确率+全信号评估")
    enhanced_bt_parser.add_argument("--config", required=True, help="配置文件路径")
    enhanced_bt_parser.add_argument("--stock", required=True, help="股票代码，如 601698")
    enhanced_bt_parser.add_argument("--days", type=int, default=60, help="回测天数（默认60）")
    enhanced_bt_parser.add_argument("--notify", action="store_true", help="把回测结果发送到飞书")

    # ── 交易日志 ──
    journal_stats_parser = subparsers.add_parser("journal-stats", help="查看交易日志统计")
    journal_stats_parser.add_argument("--config", required=True, help="配置文件路径")
    journal_stats_parser.add_argument("--notify", action="store_true", help="把统计发送到飞书")

    journal_verify_parser = subparsers.add_parser("journal-verify", help="事后验证一笔交易")
    journal_verify_parser.add_argument("--config", required=True, help="配置文件路径")
    journal_verify_parser.add_argument("--entry-id", required=True, help="交易 entry_id")
    journal_verify_parser.add_argument("--verdict", required=True, choices=["good", "bad", "neutral"], help="评价")
    journal_verify_parser.add_argument("--lessons", required=True, help="经验教训")

    journal_strategy_parser = subparsers.add_parser("journal-strategies", help="按策略统计交易表现")
    journal_strategy_parser.add_argument("--config", required=True, help="配置文件路径")
    journal_strategy_parser.add_argument("--notify", action="store_true", help="把统计发送到飞书")

    # ── 数据驱动复盘 ──
    data_review_parser = subparsers.add_parser("data-review", help="生成数据驱动的量价复盘（不依赖LLM）")
    data_review_parser.add_argument("--config", required=True, help="配置文件路径")
    data_review_parser.add_argument("--date", help="复盘日期 YYYY-MM-DD，默认今天")
    data_review_parser.add_argument("--notify", action="store_true", help="发送到飞书")
    data_review_parser.add_argument("--save-doc", action="store_true", help="更新飞书复盘文档")

    # ── 增强回测（上面已定义）──


    import_snap_parser = subparsers.add_parser("import-snapshot", help="从券商App复制文本解析并更新持仓快照")
    import_snap_parser.add_argument("--snapshot", required=True, help="目标持仓快照 JSON 文件路径")
    import_snap_parser.add_argument("--text", help="持仓文本（不传则从 stdin 读取）")
    import_snap_parser.add_argument("--date", help="交易日期 YYYY-MM-DD，默认今天")
    import_snap_parser.add_argument("--dry-run", action="store_true", help="仅打印解析结果，不写入文件")

    # ── 多Agent辩论 ──
    multi_agent_parser = subparsers.add_parser("multi-agent-debate", help="多Agent（猎手/风控/判官）仲裁辩论")
    multi_agent_parser.add_argument("--config", required=True, help="配置文件路径")
    multi_agent_parser.add_argument("--period", default="morning", choices=["morning", "close"], help="盘前/收盘")

    args = parser.parse_args()

    if args.command == "monitor-once":
        run_monitor_once(args.config, args.notify, args.mobile)
    elif args.command == "monitor-daemon":
        run_monitor_daemon(args.config)
    elif args.command == "portfolio-report":
        run_portfolio_report(args.config, args.snapshot, args.notify)
    elif args.command == "replay-signals":
        run_replay_signals(args.config, args.symbol, args.level, args.action, args.notify)
    elif args.command == "mobile-brief":
        run_mobile_brief(args.config, args.notify)
    elif args.command == "market-scan":
        run_market_scan(args.config, args.mobile, args.notify)
    elif args.command == "serve-feishu-bot":
        run_feishu_bot(args.config)
    elif args.command == "record-fill":
        run_record_fill(args.snapshot, args.side, args.code, args.quantity, args.price, args.config)
    elif args.command == "init-trading-plan":
        run_init_trading_plan(args.config)
    elif args.command == "validate-config":
        run_validate_config(args.config)
    elif args.command == "flush-failed-notifications":
        run_flush_failed_notifications()
    elif args.command == "pull-codex-notifications":
        run_pull_codex_notifications(args.limit, args.keep_unread)
    elif args.command == "close-review":
        run_close_review(args.config, args.notify)
    elif args.command == "advice-at":
        run_advice_at(args.config, args.at, args.symbol or [], args.mobile, args.notify)
    elif args.command == "compare-at":
        run_compare_at(args.config, args.from_time, args.to_time, args.symbol or [], args.mobile, args.notify)
    elif args.command == "backtest-minutes":
        run_backtest_minutes(args.config, args.days, args.symbol or [], args.mobile, args.notify)
    elif args.command == "optimize-thresholds":
        run_optimize_thresholds(args.config, args.days, args.symbol or [], args.mobile, args.notify, args.apply)
    elif args.command == "habit-profile":
        run_habit_profile(args.config, args.mobile)
    elif args.command == "status":
        run_status(args.config)
    elif args.command == "cash-deploy":
        run_cash_deploy(args.config, args.mobile, args.notify)
    elif args.command == "backtest-daily":
        run_backtest_daily(args.config, args.stock, args.days, args.mobile, args.notify)
    elif args.command == "backtest-enhanced":
        run_enhanced_backtest(args.config, args.stock, args.days, args.notify)
    elif args.command == "journal-stats":
        run_journal_stats(args.config, args.notify)
    elif args.command == "journal-verify":
        run_journal_verify(args.config, args.entry_id, args.verdict, args.lessons)
    elif args.command == "journal-strategies":
        run_journal_strategies(args.config, args.notify)
    elif args.command == "multi-agent-debate":
        run_multi_agent_debate(args.config, args.period)
    elif args.command == "data-review":
        run_data_review(args.config, args.date, args.notify, args.save_doc)
    elif args.command == "import-snapshot":
        run_import_snapshot(args.snapshot, args.text, args.date, args.dry_run)
    elif args.command == "prune-data":
        run_prune_data(args.config, args.retention_days)


def run_monitor_once(config_path: str, force_notify: bool, mobile: bool) -> None:
    config = require_valid_config(config_path)
    conn = connect_db(config.storage.sqlite_path)
    portfolio_snapshot = _load_portfolio_snapshot(config)
    cash_ratio = compute_cash_ratio(portfolio_snapshot)
    benchmark_history = _load_benchmark_history(config)
    trading_habit_profile = build_trading_habit_profile(conn)
    provider = build_provider(config)
    advance_ratio, rank_map, sector_boards = load_market_context(config)
    volatile_period = is_high_volatility_period()

    for stock in config.monitor.stocks:
        history = load_stock_history(config, conn, provider, stock)
        if not history:
            continue
        quote = history[-1]
        holding = find_holding(portfolio_snapshot, stock.code)
        if holding is not None and holding.quantity <= 0:
            continue
        result = analyze_quotes(
            history,
            config.monitor,
            portfolio_holding=holding,
            benchmark_history=benchmark_history,
            trading_habit_profile=trading_habit_profile,
            market_advance_ratio=advance_ratio,
            hot_stock_rank=rank_map.get(stock.code, 0),
            is_volatile_period=volatile_period,
            portfolio_cash_ratio=cash_ratio,
            sector_boards=sector_boards,
            portfolio_position_ratio=compute_position_ratio(portfolio_snapshot, holding, history[-1].current_price),
        )
        print("=" * 80)
        rendered = format_mobile_signal(result.title, result.message) if mobile else result.message
        if not mobile:
            print(result.title)
        print(rendered)
        persist_observation(conn, quote, result)
        if force_notify or result.should_notify or config.monitor.notification.notify_on_neutral:
            if config.monitor.notification.feishu.enabled:
                payload = format_mobile_signal(result.title, result.message, include_title=False) if mobile else result.message
                deliver_feishu_message(
                    config.monitor.notification.feishu,
                    result.title,
                    payload,
                    app_id=config.feishu_bot.app_id,
                    app_secret=config.feishu_bot.app_secret,
                )


def run_monitor_daemon(config_path: str) -> None:
    config = require_valid_config(config_path)
    runtime = MonitorRuntime(config)
    runtime.serve_forever()


def run_portfolio_report(config_path: str, snapshot_path: str, notify: bool) -> None:
    config = require_valid_config(config_path)
    snapshot, saved_path, report = generate_portfolio_report(snapshot_path, config.portfolio.data_dir)
    print(report)
    print(f"\n[saved] {saved_path}")

    if notify and config.monitor.notification.feishu.enabled:
        deliver_feishu_message(
            config.monitor.notification.feishu,
            f"收盘持仓建议 {snapshot.trade_date.isoformat()}",
            report,
        )


def run_replay_signals(
    config_path: str,
    symbol: str | None,
    level: str | None,
    action: str | None,
    notify: bool,
) -> None:
    config = require_valid_config(config_path)
    conn = connect_db(config.storage.sqlite_path)
    stats = replay_signal_stats(conn, symbol=symbol, signal_level=level, action=action)
    rendered = format_mobile_replay(stats, symbol=symbol, level=level, action=action)
    print(rendered)
    if notify and config.monitor.notification.feishu.enabled:
        deliver_feishu_message(
            config.monitor.notification.feishu,
            "历史回放统计",
            rendered,
            app_id=config.feishu_bot.app_id,
            app_secret=config.feishu_bot.app_secret,
        )


def _load_positions_for_briefing(config) -> dict[str, int]:
    """Load current position quantities from portfolio snapshot for briefing display."""
    try:
        from .portfolio import load_portfolio_snapshot
        snap = load_portfolio_snapshot(config.snapshot_path)
        return {h.code: h.quantity for h in snap.holdings if h.quantity > 0}
    except Exception:
        return {}


def run_mobile_brief(config_path: str, notify: bool) -> None:
    config = require_valid_config(config_path)
    conn = connect_db(config.storage.sqlite_path)
    positions = _load_positions_for_briefing(config)
    rendered = format_mobile_digest(fetch_latest_briefing(conn), positions=positions)
    print(rendered)
    if notify and config.monitor.notification.feishu.enabled:
        deliver_feishu_message(
            config.monitor.notification.feishu,
            "AI股票决策简报",
            rendered,
            app_id=config.feishu_bot.app_id,
            app_secret=config.feishu_bot.app_secret,
        )


def run_market_scan(config_path: str, mobile: bool, notify: bool) -> None:
    config = require_valid_config(config_path)
    rendered = render_market_overview(build_market_overview(config), mobile=mobile)
    print(rendered)
    if notify and config.monitor.notification.feishu.enabled:
        deliver_feishu_message(
            config.monitor.notification.feishu,
            "全市场扫描",
            rendered,
            app_id=config.feishu_bot.app_id,
            app_secret=config.feishu_bot.app_secret,
        )


def run_feishu_bot(config_path: str) -> None:
    config = require_valid_config(config_path)
    serve_feishu_bot(config)


def run_record_fill(snapshot_path: str, side: str, code: str, quantity: int, price: str, config_path: str | None) -> None:
    price_decimal = Decimal(price)
    before_snapshot = load_trade_snapshot(snapshot_path)
    before_holding = next((item for item in before_snapshot.holdings if item.code == code), None)
    before_quantity = before_holding.quantity if before_holding is not None else 0
    snapshot = apply_trade_fill(snapshot_path, side, code, quantity, price_decimal, persist=False)
    after_holding = next((item for item in snapshot.holdings if item.code == code), None)
    after_quantity = after_holding.quantity if after_holding is not None else 0
    learned_profile_rendered = _record_fill_and_render_habit_profile(
        snapshot_path,
        snapshot,
        side,
        code,
        quantity,
        price_decimal,
        before_quantity,
        after_quantity,
        config_path,
    )
    print(f"已更新持仓：{side} {code} {quantity} 股 @ {price}")
    print(f"最新总资产：{snapshot.total_assets}")
    print(f"最新现金：{snapshot.cash}")
    print("")
    print(build_post_fill_execution_sheet(snapshot))
    if learned_profile_rendered:
        print("")
        print(learned_profile_rendered)


def run_init_trading_plan(config_path: str) -> None:
    config = load_config(config_path)
    path = ensure_trigger_file(config.trading_plan.path)
    print(f"已生成默认交易计划文件：{path}")


def run_validate_config(config_path: str) -> None:
    errors = validate_config(config_path)
    if errors:
        print("配置校验失败：")
        for error in errors:
            print(f"- {error}")
        raise SystemExit(1)
    print("配置校验通过")


def run_flush_failed_notifications() -> None:
    sent_count, pending_count = flush_failed_notifications()
    if sent_count:
        print(f"已重放失败通知: {sent_count}")
    elif pending_count:
        print(f"仍有失败通知待重放: {pending_count}")
    else:
        print("没有待重放的失败通知")


def run_pull_codex_notifications(limit: int, keep_unread: bool) -> None:
    items = pull_codex_notifications(limit=max(limit, 1), mark_sent=not keep_unread)
    if not items:
        print("没有未读的 Codex 推送")
        return

    for idx, item in enumerate(items, start=1):
        if idx > 1:
            print("\n" + ("=" * 80))
        print(f"[{item['created_at']}] {item['title']}")
        print("")
        print(item["message"])


def run_close_review(config_path: str, notify: bool) -> None:
    config = require_valid_config(config_path)
    artifact = build_close_review(config)
    print(artifact.body)
    print(f"\n[saved] {artifact.saved_path}")
    
    # Run self-feedback loop: evaluate today's debate predictions
    try:
        from .feedback_loop import run_daily_feedback
        from decimal import Decimal
        prices = {}
        for h in config.monitor.holdings:
            if h.current_price and h.current_price > 0:
                prices[h.symbol] = h.current_price
        if prices:
            feedbacks = run_daily_feedback(prices)
            correct = sum(1 for f in feedbacks if f.was_correct)
            total = len(feedbacks)
            if total > 0:
                print(f"\n[feedback] 今日辩论验证: {correct}/{total} 命中 ({correct/total*100:.0f}%)")
    except Exception:
        pass  # Feedback is best-effort
    
    if notify and config.monitor.notification.feishu.enabled:
        deliver_feishu_message(
            config.monitor.notification.feishu,
            artifact.title,
            artifact.body,
            app_id=config.feishu_bot.app_id,
            app_secret=config.feishu_bot.app_secret,
        )


def run_advice_at(config_path: str, at_text: str, symbols: list[str], mobile: bool, notify: bool) -> None:
    config = require_valid_config(config_path)
    requested_at = parse_history_datetime(at_text)
    stocks = [_resolve_stock_ref(config, symbol) for symbol in symbols] if symbols else None
    items = analyze_historical_point(config, requested_at, stocks=stocks)
    rendered = render_historical_advice(items, mobile=mobile)
    print(rendered)
    if notify and config.monitor.notification.feishu.enabled:
        deliver_feishu_message(
            config.monitor.notification.feishu,
            f"历史时点建议 {requested_at:%Y-%m-%d %H:%M}",
            rendered,
            app_id=config.feishu_bot.app_id,
            app_secret=config.feishu_bot.app_secret,
        )


def run_compare_at(
    config_path: str,
    from_text: str,
    to_text: str,
    symbols: list[str],
    mobile: bool,
    notify: bool,
) -> None:
    config = require_valid_config(config_path)
    start_at = parse_history_datetime(from_text)
    end_at = parse_history_datetime(to_text)
    stocks = [_resolve_stock_ref(config, symbol) for symbol in symbols] if symbols else None
    items = compare_historical_points(config, start_at, end_at, stocks=stocks)
    rendered = render_historical_compare(items, mobile=mobile)
    print(rendered)
    if notify and config.monitor.notification.feishu.enabled:
        deliver_feishu_message(
            config.monitor.notification.feishu,
            f"历史时点对比 {start_at:%Y-%m-%d %H:%M} -> {end_at:%Y-%m-%d %H:%M}",
            rendered,
        )


def run_backtest_minutes(config_path: str, days: int, symbols: list[str], mobile: bool, notify: bool) -> None:
    config = require_valid_config(config_path)
    stocks = [_resolve_stock_ref(config, symbol) for symbol in symbols] if symbols else None
    stats = run_minute_backtest(config, symbols=stocks, ndays=days)
    rendered = render_minute_backtest(stats, mobile=mobile)
    print(rendered)
    if notify and config.monitor.notification.feishu.enabled:
        deliver_feishu_message(
            config.monitor.notification.feishu,
            f"分钟级回测 最近{days}日",
            rendered,
        )


def run_optimize_thresholds(config_path: str, days: int, symbols: list[str], mobile: bool, notify: bool, apply: bool = False) -> None:
    config = require_valid_config(config_path)
    stocks = [_resolve_stock_ref(config, symbol) for symbol in symbols] if symbols else None
    report = optimize_decision_thresholds(config, symbols=stocks, ndays=days)
    rendered = render_optimization_report(report, mobile=mobile)
    print(rendered)
    if apply and not report.get("keep_current") and report.get("recommended"):
        best = report["recommended"][0]
        _apply_thresholds_to_config(config_path, best["buy_score"], best["hold_score"], best["reduce_score"])
        print(f"\n[已写入] buy_score={best['buy_score']} hold_score={best['hold_score']} reduce_score={best['reduce_score']} → {config_path}")
    elif apply:
        print("\n[跳过] 当前阈值已是最优或样本不足，未写入")
    if notify and config.monitor.notification.feishu.enabled:
        deliver_feishu_message(
            config.monitor.notification.feishu,
            f"阈值优化建议 最近{days}日",
            rendered,
        )


def run_habit_profile(config_path: str, mobile: bool) -> None:
    config = require_valid_config(config_path)
    conn = connect_db(config.storage.sqlite_path)
    print(render_trading_habit_profile(build_trading_habit_profile(conn), mobile=mobile))


def run_import_snapshot(snapshot_path: str, text: str | None, trade_date_str: str | None, dry_run: bool) -> None:
    import sys
    from .models import PortfolioHolding, PortfolioSnapshot

    raw_text = text if text is not None else sys.stdin.read()
    result = parse_portfolio_text(raw_text)

    for w in result.warnings:
        print(f"[警告] {w}")

    if not result.holdings:
        print("解析失败：未找到持仓数据")
        return

    trade_date = date.fromisoformat(trade_date_str) if trade_date_str else date.today()
    snap_path = Path(snapshot_path)

    existing_total = result.total_assets
    existing_cash = result.cash
    if snap_path.exists() and (existing_total is None or existing_cash is None):
        try:
            old = load_snapshot(snap_path)
            if existing_total is None:
                existing_total = old.total_assets
            if existing_cash is None:
                existing_cash = old.cash
        except Exception as exc:
            logger.warning("Stale snapshot read failed error=%s", exc)

    total_assets = existing_total or Decimal("0")
    cash = existing_cash or Decimal("0")

    holdings = [
        PortfolioHolding(
            name=h.name,
            code=h.code,
            quantity=h.quantity,
            cost_price=h.cost_price,
            current_price=h.current_price,
        )
        for h in result.holdings
    ]
    snapshot = PortfolioSnapshot(trade_date=trade_date, total_assets=total_assets, cash=cash, holdings=holdings)

    print(f"解析结果（{trade_date}）：")
    print(f"  总资产：{total_assets}  现金：{cash}")
    for h in holdings:
        print(f"  {h.name}({h.code})  {h.quantity}股  成本{h.cost_price}  现价{h.current_price}")

    if dry_run:
        print("\n[dry-run] 未写入文件")
        return

    import json
    payload = {
        "tradeDate": snapshot.trade_date.isoformat(),
        "totalAssets": float(snapshot.total_assets),
        "cash": float(snapshot.cash),
        "holdings": [
            {"name": h.name, "code": h.code, "quantity": h.quantity,
             "costPrice": float(h.cost_price), "currentPrice": float(h.current_price)}
            for h in snapshot.holdings
        ],
    }
    snap_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {snap_path}")


def _resolve_stock_ref(config, query: str) -> StockRef:
    for stock in config.monitor.stocks:
        if stock.code == query or stock.symbol == normalized:
            return stock
    if len(normalized) == 6 and normalized.isdigit():
        exchange = "sh" if normalized.startswith(("5", "6", "9")) else "sz"
        return StockRef(exchange=exchange, code=normalized)
    raise RuntimeError(f"无法识别股票代码: {query}")


def _apply_thresholds_to_config(config_path: str, buy_score: int, hold_score: int, reduce_score: int) -> None:
    import re
    text = Path(config_path).read_text(encoding="utf-8")
    text = re.sub(r"(buy_score:\s*)\d+", f"\\g<1>{buy_score}", text)
    text = re.sub(r"(hold_score:\s*)\d+", f"\\g<1>{hold_score}", text)
    text = re.sub(r"(reduce_score:\s*)\d+", f"\\g<1>{reduce_score}", text)
    Path(config_path).write_text(text, encoding="utf-8")




def _load_portfolio_snapshot(config):
    if not config.snapshot_path.exists():
        return None
    return load_snapshot(config.snapshot_path)


def _load_benchmark_history(config):
    benchmark = config.monitor.benchmark
    if benchmark is None:
        return None
    provider = build_provider(config)
    if config.monitor.provider == "eastmoney_minute":
        return provider.fetch_recent_window(benchmark, config.monitor.history_size)
    try:
        return [provider.fetch_quote(benchmark)]
    except Exception as exc:
        logger.warning("Benchmark fetch failed error=%s", exc)
        return None






def _record_fill_and_render_habit_profile(
    snapshot_path: str,
    snapshot,
    side: str,
    code: str,
    quantity: int,
    price: Decimal,
    before_quantity: int,
    after_quantity: int,
    config_path: str | None,
) -> str | None:
    resolved_config_path = config_path
    if resolved_config_path is None:
        default_config = "config.yaml"
        if Path(default_config).exists():
            resolved_config_path = default_config
    if resolved_config_path is None:
        save_trade_snapshot(snapshot_path, snapshot)
        return None
    config = require_valid_config(resolved_config_path)
    conn = connect_db(config.storage.sqlite_path)
    try:
        with conn:
            insert_trade_fill(
                conn,
                TradeFillRecord(
                    side=side,
                    code=code,
                    quantity=quantity,
                    price=price,
                    before_quantity=before_quantity,
                    after_quantity=after_quantity,
                    filled_at=datetime.now(),
                ),
            )
            save_trade_snapshot(snapshot_path, snapshot)
        return render_trading_habit_profile(build_trading_habit_profile(conn), mobile=True)
    finally:
        conn.close()


def run_status(config_path: str) -> None:
    """Print current system status: trading calendar, holdings, triggers."""
    config = require_valid_config(config_path)
    from datetime import datetime as dt
    from .market_hours import MARKET_TZ, is_a_share_trading_time, is_auction_period

    now = dt.now(MARKET_TZ)
    is_trading = is_a_share_trading_time(now)
    is_auction = is_auction_period(now)
    next_sess = next_session_str(now)

    print(f"当前时间：{now.strftime('%Y-%m-%d %H:%M')} {['周一','周二','周三','周四','周五','周六','周日'][now.weekday()]}")
    print(f"交易时段：{'是' if is_trading else '否'}{'（集合竞价）' if is_auction else ''}")
    print(f"下次开盘：{next_sess}")
    print()

    # Holdings summary
    snapshot_path = config.snapshot_path
    if snapshot_path.exists():
        snapshot = load_snapshot(snapshot_path)
        print(f"总资产：{snapshot.total_assets:.0f}  现金：{snapshot.cash:.0f}（{(snapshot.cash/snapshot.total_assets*100):.0f}%）")
        print()
        print("持仓：")
        for h in snapshot.holdings:
            if h.quantity <= 0:
                continue
            pnl = ((h.current_price - h.cost_price) / h.cost_price * 100) if h.cost_price > 0 else 0
            mkt_val = h.current_price * h.quantity
            print(f"  {h.name}({h.code})：{h.quantity}股 | 成本 {h.cost_price} | 现价 {h.current_price} | 盈亏 {pnl:+.1f}% | 市值 {mkt_val:.0f}")

        # Active triggers
        triggers = load_triggers(config.trading_plan.path)
        if triggers:
            active_codes = {h.code for h in snapshot.holdings if h.quantity > 0}
            active = [t for t in triggers.values() if t.code in active_codes]
            orphan = [t for t in triggers.values() if t.code not in active_codes]
            if active:
                print(f"\n活跃触发单：")
                for t in active:
                    print(f"  {t.code} {t.name}：{t.action} {t.quantity}股 @ {t.price_min}-{t.price_max}（回落 {t.fallback_price}）")
            if orphan:
                print(f"\n⚠️ 已清仓触发单：")
                for t in orphan:
                    print(f"  {t.code} {t.name}：已清仓但触发单仍存在，建议清理")
    else:
        print("⚠️ 持仓快照不存在")

    # Daemon status
    import subprocess
    try:
        result = subprocess.run(["pgrep", "-f", "monitor-daemon"], capture_output=True, text=True)
        if result.stdout.strip():
            print(f"\n✅ daemon 运行中")
        else:
            print(f"\n❌ daemon 未运行")
    except Exception:
        print(f"\n⚠️ 无法检测 daemon 状态")

    # Latest pre-market briefing
    import json
    briefing_path = Path("data/briefing/latest.json")
    if briefing_path.exists():
        b = json.loads(briefing_path.read_text())
        print(f"\n最近盘前简报：{b['date']}（{b['generated_at'][:16]}）")
        # Print just the quick verdict lines
        summary = b.get("summary", "")
        verdict_start = summary.find("【今日速判】")
        if verdict_start > 0:
            print(summary[verdict_start:].split("\n下次开盘")[0])


def run_prune_data(config_path: str, retention_days: int) -> None:
    """Delete data older than retention_days from the database."""
    config = require_valid_config(config_path)
    conn = connect_db(config.storage.sqlite_path)
    try:
        result = prune_old_data(conn, retention_days=retention_days)
        parts = [f"{table}={count}" for table, count in result.items()]
        print(f"✅ 已清理 {retention_days} 天前的数据: {', '.join(parts)}")
    finally:
        conn.close()


def run_cash_deploy(config_path: str, mobile: bool, notify: bool) -> None:
    """检查现金部署条件，输出可入场标的和买入建议。"""
    config = require_valid_config(config_path)
    signal = generate_deploy_signal(config, mobile=mobile)
    rendered = render_deploy_signal(signal, mobile=mobile)
    print(rendered)
    if notify:
        notify_feishu_if_enabled(config, f"【现金部署信号】\n{rendered}")


def run_backtest_daily(config_path: str, stock_code: str, days: int, mobile: bool, notify: bool) -> None:
    """日线级别单股回测。"""
    config = require_valid_config(config_path)
    # Determine exchange prefix from code
    code = stock_code
    exchange = "sh" if code.startswith(("6", "5")) else "sz"
    stock = StockRef(exchange, code)
    result = run_daily_backtest(config, stock, days=days)
    lines = [result.summary]
    if not mobile:
        lines.append("")
        buy_signals = [s for s in result.daily_returns if s["action"] == "buy"]
        if buy_signals:
            lines.append(f"买入信号明细（共 {len(buy_signals)} 次）：")
            for s in buy_signals[-10:]:
                mark = "✅" if s["next_day_return"] > 0 else "❌"
                lines.append(
                    f"  {mark} {s['date']} | 评分 {s['score']:.0f} | 收盘 {s['close']:.2f} | 次日回报 {s['next_day_return']:+.2f}%"
                )
        lines.append("")
        lines.append("仅供参考，不构成投资建议")
    rendered = "\n".join(lines)
    print(rendered)
    if notify:
        notify_feishu_if_enabled(config, f"【日线回测】{stock_code}\n{rendered}")


def run_enhanced_backtest(config_path: str, stock_code: str, days: int, notify: bool) -> None:
    """增强回测：多窗口方向准确率 + 全信号评估。"""
    config = require_valid_config(config_path)
    code = stock_code
    exchange = "sh" if code.startswith(("6", "5")) else "sz"
    stock = StockRef(exchange, code)

    result = run_enhanced_daily_backtest(config, stock, days=days)

    # Build output
    lines = [result["summary"], ""]

    # Detailed signal table (last 10)
    signals = result["signals"]
    if signals:
        lines.append(f"最近 10 条信号明细：")
        lines.append(f"{'日期':<12} {'动作':<8} {'评分':<6} {'1日':>7} {'3日':>7} {'5日':>7}")
        for s in signals[-10:]:
            r1 = f"{s.get('return_1d'):+.2f}%" if s.get('return_1d') is not None else "  N/A"
            r3 = f"{s.get('return_3d'):+.2f}%" if s.get('return_3d') is not None else "  N/A"
            r5 = f"{s.get('return_5d'):+.2f}%" if s.get('return_5d') is not None else "  N/A"
            d1 = {"win": "✅", "loss": "❌", "neutral": "➖"}.get(s.get("direction_1d"), "  ")
            d3 = {"win": "✅", "loss": "❌", "neutral": "➖"}.get(s.get("direction_3d"), "  ")
            d5 = {"win": "✅", "loss": "❌", "neutral": "➖"}.get(s.get("direction_5d"), "  ")
            lines.append(
                f"{s['date']:<12} {s['action']:<8} {s['score']:<6.0f} "
                f"{d1}{r1:>6} {d3}{r3:>6} {d5}{r5:>6}"
            )

    lines.append("")
    lines.append("方向判定：buy→涨=✅ | reduce→跌=✅ | hold→不跌=✅ | avoid→不涨=✅")
    lines.append("仅供参考，不构成投资建议")

    rendered = "\n".join(lines)
    print(rendered)
    if notify:
        notify_feishu_if_enabled(config, f"【增强回测】{stock_code}\n{rendered}")


def run_journal_stats(config_path: str, notify: bool) -> None:
    """查看交易日志统计。"""
    config = require_valid_config(config_path)
    journal = TradeJournal(Path(config.portfolio.data_dir) / "trade_journal")
    stats = journal.get_stats()
    lines = [
        "【交易日志统计】",
        f"总交易笔数：{stats['total_trades']}",
        f"买入：{stats['total_buys']} | 卖出：{stats['total_sells']}",
        f"已验证交易：{stats['verified_trades']}",
        f"好交易：{stats['good_trades']} | 坏交易：{stats['bad_trades']}",
        f"胜率：{stats['win_rate']:.1f}%",
        f"累计盈亏：{stats['total_pnl_pct']:+.2f}%",
        f"平均持仓天数：{stats['avg_holding_days']}天",
    ]
    rendered = "\n".join(lines)
    print(rendered)
    if notify:
        notify_feishu_if_enabled(config, rendered)


def run_journal_verify(config_path: str, entry_id: str, verdict: str, lessons: str) -> None:
    """事后验证一笔交易。"""
    config = require_valid_config(config_path)
    journal = TradeJournal(Path(config.portfolio.data_dir) / "trade_journal")
    ok = journal.verify_trade(entry_id, verdict, lessons)
    if ok:
        print(f"✅ 已验证交易 {entry_id}：{verdict}")
    else:
        print(f"❌ 未找到交易 {entry_id}")


def run_journal_strategies(config_path: str, notify: bool) -> None:
    """按策略统计交易表现。"""
    config = require_valid_config(config_path)
    journal = TradeJournal(Path(config.portfolio.data_dir) / "trade_journal")
    stats = journal.get_strategy_stats()

    if not stats:
        print("暂无策略统计（需要先有交易记录并事后验证）")
        return

    lines = ["【策略有效性统计】", ""]
    lines.append(f"{'策略':<20} {'交易':>4} {'验证':>4} {'好':>4} {'坏':>4} {'胜率':>7} {'盈亏':>8} {'均持':>5}")
    lines.append("-" * 65)

    for strategy, s in stats.items():
        emoji = "🟢" if s["win_rate"] >= 60 else ("🟡" if s["win_rate"] >= 40 else "🔴")
        lines.append(
            f"{strategy:<20} {s['total']:>4} {s['verified']:>4} {s['good']:>4} {s['bad']:>4} "
            f"{emoji}{s['win_rate']:>5.0f}% {s['total_pnl_pct']:>+7.1f}% {s['avg_holding_days']:>4.0f}天"
        )

    lines.append("")
    lines.append("🟢 胜率≥60% → 可继续使用  🟡 40-60% → 观察  🔴 <40% → 需调整或停用")

    rendered = "\n".join(lines)
    print(rendered)
    if notify:
        notify_feishu_if_enabled(config, rendered)


def run_data_review(config_path: str, trade_date: str | None, notify: bool, save_doc: bool) -> None:
    """Generate data-driven review from market.db."""
    config = require_valid_config(config_path)
    if trade_date is None:
        from datetime import datetime as dt
        trade_date = dt.now().strftime("%Y-%m-%d")

    # Load holdings from snapshot
    from .portfolio import load_snapshot
    snapshot_path = config.snapshot_path
    stocks = []
    if snapshot_path.exists():
        snapshot = load_snapshot(snapshot_path)
        for h in snapshot.holdings:
            if h.quantity > 0:
                stocks.append({
                    "code": h.code,
                    "name": h.name,
                    "cost": float(h.cost_price),
                    "shares": int(h.quantity),
                })

    if not stocks:
        print("No active holdings found.")
        return

    review_full, review_mobile = generate_data_review(
        config.storage.sqlite_path, stocks, trade_date=trade_date
    )
    print(review_full)

    if notify:
        from .notify import deliver_feishu_message
        # 发短摘要到飞书（不会截断）
        deliver_feishu_message(
            config.monitor.notification.feishu,
            f"📊 {trade_date} 数据复盘",
            review_mobile,
            app_id=config.feishu_bot.app_id,
            app_secret=config.feishu_bot.app_secret,
        )
        print("\n✅ 短摘要已发送到飞书")

    if save_doc:
        _save_review_to_feishu_doc(review_full, trade_date)


def _save_review_to_feishu_doc(content: str, trade_date: str) -> None:
    """Append data review section to the Feishu review document."""
    import json
    import re
    import subprocess
    import tempfile
    from pathlib import Path

    REVIEW_DOC_TOKEN = "F2Uidqo8ZoaVDZxA0tWcoLo4nvf"

    # 1. Fetch existing doc
    result = subprocess.run(
        ["lark-cli", "docs", "+fetch", "--doc", REVIEW_DOC_TOKEN, "--format", "markdown"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"❌ lark-cli fetch failed: {result.stderr[:200]}")
        return

    try:
        doc = json.loads(result.stdout)
        old_md = doc.get("data", {}).get("markdown", "")
    except json.JSONDecodeError:
        old_md = result.stdout

    # 2. Merge: new content first, then existing (remove the same date if already present)
    date_header = f"## {trade_date.replace('-', '.')}"
    if date_header in old_md:
        # Remove existing section for this date
        old_md = re.sub(
            rf"{re.escape(date_header)}.*?(?=\n## \d|\Z)",
            "",
            old_md,
            flags=re.DOTALL,
        )
    new_md = content + "\n\n" + old_md.strip()

    # 3. Write to project data dir (lark-cli requires relative path)
    out_path = Path("data") / f"review_{trade_date.replace('-', '')}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(new_md, encoding="utf-8")

    try:
        result = subprocess.run(
            ["lark-cli", "docs", "+update", "--doc", REVIEW_DOC_TOKEN,
             "--mode", "overwrite", "--markdown", f"@./{out_path}"],
            capture_output=True, text=True, timeout=60, cwd="/root/projects/stock-advisor-bot",
        )
        if result.returncode == 0:
            print("✅ 已写入飞书复盘文档")
        else:
            print(f"❌ lark-cli update failed: {result.stderr[:200]}")
    finally:
        out_path.unlink(missing_ok=True)


def run_multi_agent_debate(config_path: str, period: str) -> None:
    """Run multi-agent debate on all holdings (猎手/风控/判官 → 仲裁)."""
    config = require_valid_config(config_path)
    from .multi_agent_briefing import run_multi_agent_briefing
    report = run_multi_agent_briefing(config, period=period)
    print(report)


if __name__ == "__main__":
    main()
