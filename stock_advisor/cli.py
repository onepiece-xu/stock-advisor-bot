from __future__ import annotations

import argparse

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .config import load_config, require_valid_config, validate_config
from .habit_learning import build_trading_habit_profile, render_trading_habit_profile
from .backtest import (
    optimize_decision_thresholds,
    render_minute_backtest,
    render_optimization_report,
    run_minute_backtest,
)
from .briefing import format_mobile_digest, format_mobile_replay, format_mobile_signal
from .feishu_bot_server import serve_feishu_bot
from .market_overview import build_market_overview, render_market_overview
from .historical import (
    analyze_historical_point,
    compare_historical_points,
    render_historical_advice,
    render_historical_compare,
)
from .models import StockRef, TradeFillRecord
from .notify import deliver_feishu_message, flush_failed_notifications
from .portfolio import build_daily_report, find_holding, load_previous_snapshot, load_snapshot, save_snapshot
from .market_hours import is_high_volatility_period
from .providers import EastmoneyMarketSnapshotProvider, EastmoneyMinuteHistoryProvider, TencentQuoteProvider
from .analysis import analyze_quotes
from .review import build_close_review
from .runtime import MonitorRuntime
from .storage import (
    cache_quotes,
    connect_db,
    fetch_latest_briefing,
    insert_trade_fill,
    load_recent_quotes,
    persist_observation,
    replay_signal_stats,
)
from .trading_plan import (
    apply_trade_fill,
    build_post_fill_execution_sheet,
    ensure_trigger_file,
    load_snapshot as load_trade_snapshot,
    save_snapshot as save_trade_snapshot,
)


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


def run_monitor_once(config_path: str, force_notify: bool, mobile: bool) -> None:
    config = require_valid_config(config_path)
    conn = connect_db(config.storage.sqlite_path)
    portfolio_snapshot = _load_portfolio_snapshot(config)
    cash_ratio = _compute_cash_ratio(portfolio_snapshot)
    benchmark_history = _load_benchmark_history(config)
    trading_habit_profile = build_trading_habit_profile(conn)
    provider = _build_provider(config)
    advance_ratio, rank_map, sector_boards = _load_market_context(config)
    volatile_period = is_high_volatility_period()

    for stock in config.monitor.stocks:
        history = _load_stock_history(config, conn, provider, stock)
        if not history:
            continue
        quote = history[-1]
        holding = find_holding(portfolio_snapshot, stock.code)
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
            portfolio_position_ratio=_compute_position_ratio(portfolio_snapshot, holding, history[-1].current_price),
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
                deliver_feishu_message(config.monitor.notification.feishu, result.title, payload)


def run_monitor_daemon(config_path: str) -> None:
    config = require_valid_config(config_path)
    runtime = MonitorRuntime(config)
    runtime.serve_forever()


def run_portfolio_report(config_path: str, snapshot_path: str, notify: bool) -> None:
    config = require_valid_config(config_path)
    snapshot = load_snapshot(snapshot_path)
    previous = load_previous_snapshot(config.portfolio.data_dir, snapshot.trade_date)
    saved_path = save_snapshot(snapshot, config.portfolio.data_dir)
    report = build_daily_report(snapshot, previous)
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
        deliver_feishu_message(config.monitor.notification.feishu, "历史回放统计", rendered)


def run_mobile_brief(config_path: str, notify: bool) -> None:
    config = require_valid_config(config_path)
    conn = connect_db(config.storage.sqlite_path)
    rendered = format_mobile_digest(fetch_latest_briefing(conn))
    print(rendered)
    if notify and config.monitor.notification.feishu.enabled:
        deliver_feishu_message(config.monitor.notification.feishu, "AI股票决策简报", rendered)


def run_market_scan(config_path: str, mobile: bool, notify: bool) -> None:
    config = require_valid_config(config_path)
    rendered = render_market_overview(build_market_overview(config), mobile=mobile)
    print(rendered)
    if notify and config.monitor.notification.feishu.enabled:
        deliver_feishu_message(config.monitor.notification.feishu, "全市场扫描", rendered)


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


def run_close_review(config_path: str, notify: bool) -> None:
    config = require_valid_config(config_path)
    artifact = build_close_review(config)
    print(artifact.body)
    print(f"\n[saved] {artifact.saved_path}")
    if notify and config.monitor.notification.feishu.enabled:
        deliver_feishu_message(config.monitor.notification.feishu, artifact.title, artifact.body)


def run_advice_at(config_path: str, at_text: str, symbols: list[str], mobile: bool, notify: bool) -> None:
    config = require_valid_config(config_path)
    requested_at = _parse_history_datetime(at_text)
    stocks = [_resolve_stock_ref(config, symbol) for symbol in symbols] if symbols else None
    items = analyze_historical_point(config, requested_at, stocks=stocks)
    rendered = render_historical_advice(items, mobile=mobile)
    print(rendered)
    if notify and config.monitor.notification.feishu.enabled:
        deliver_feishu_message(config.monitor.notification.feishu, f"历史时点建议 {requested_at:%Y-%m-%d %H:%M}", rendered)


def run_compare_at(
    config_path: str,
    from_text: str,
    to_text: str,
    symbols: list[str],
    mobile: bool,
    notify: bool,
) -> None:
    config = require_valid_config(config_path)
    start_at = _parse_history_datetime(from_text)
    end_at = _parse_history_datetime(to_text)
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


def _parse_history_datetime(text: str) -> datetime:
    normalized = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    raise RuntimeError(f"无法解析历史时点: {text}")


def _resolve_stock_ref(config, query: str) -> StockRef:
    normalized = query.strip().lower()
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


def _compute_cash_ratio(snapshot) -> Decimal | None:
    if snapshot is None or snapshot.total_assets <= 0:
        return None
    return (snapshot.cash / snapshot.total_assets).quantize(Decimal("0.0001"))


def _compute_position_ratio(snapshot, holding, current_price: Decimal) -> Decimal | None:
    if snapshot is None or snapshot.total_assets <= 0 or holding is None or holding.quantity <= 0:
        return None
    position_value = Decimal(str(holding.quantity)) * current_price
    return (position_value / snapshot.total_assets).quantize(Decimal("0.0001"))


def _load_market_context(config) -> tuple[Decimal, dict[str, int], list[dict]]:
    advance_ratio = Decimal("0")
    rank_map: dict[str, int] = {}
    sector_boards: list[dict] = []
    try:
        snapshot_provider = EastmoneyMarketSnapshotProvider(config.monitor)
        breadth = snapshot_provider.fetch_market_breadth()
        total = breadth.get("up_count", 0) + breadth.get("flat_count", 0) + breadth.get("down_count", 0)
        if total > 0:
            advance_ratio = Decimal(str(breadth["up_count"])) / Decimal(str(total))
        top_stocks = snapshot_provider.fetch_top_stocks(limit=50)
        rank_map = {item["code"]: idx + 1 for idx, item in enumerate(top_stocks)}
        sector_boards = snapshot_provider.fetch_sector_boards(kind="industry", limit=5) + snapshot_provider.fetch_sector_boards(kind="concept", limit=5)
    except Exception:
        pass
    return advance_ratio, rank_map, sector_boards


def _load_portfolio_snapshot(config):
    snapshot_path = config.storage.sqlite_path.resolve().parent.parent / "portfolio-snapshot.json"
    if not snapshot_path.exists():
        return None
    return load_snapshot(snapshot_path)


def _load_benchmark_history(config):
    benchmark = config.monitor.benchmark
    if benchmark is None:
        return None
    provider = _build_provider(config)
    if config.monitor.provider == "eastmoney_minute":
        return provider.fetch_recent_window(benchmark, config.monitor.history_size)
    try:
        return [provider.fetch_quote(benchmark)]
    except Exception:
        return None


def _build_provider(config):
    if config.monitor.provider == "eastmoney_minute":
        return EastmoneyMinuteHistoryProvider(config.monitor)
    return TencentQuoteProvider(config.monitor)


def _load_stock_history(config, conn, provider, stock):
    if config.monitor.provider == "eastmoney_minute":
        history = provider.fetch_recent_window(stock, config.monitor.history_size)
        if history:
            cache_quotes(conn, history)
        return history
    history = load_recent_quotes(conn, stock.symbol, config.monitor.history_size - 1)
    history.append(provider.fetch_quote(stock))
    return history


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


if __name__ == "__main__":
    main()
