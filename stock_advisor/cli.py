from __future__ import annotations

import argparse

from .config import load_config
from .briefing import format_mobile_digest, format_mobile_replay, format_mobile_signal
from .feishu_bot_server import serve_feishu_bot
from .notify import deliver_feishu_message
from .portfolio import build_daily_report, load_previous_snapshot, load_snapshot, save_snapshot
from .providers import TencentQuoteProvider
from .analysis import analyze_quotes
from .runtime import MonitorRuntime
from .storage import connect_db, fetch_latest_briefing, insert_quote, insert_signal, load_recent_quotes, replay_signal_stats


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

    bot_parser = subparsers.add_parser("serve-feishu-bot", help="启动飞书机器人命令回调服务")
    bot_parser.add_argument("--config", required=True, help="配置文件路径")

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
    elif args.command == "serve-feishu-bot":
        run_feishu_bot(args.config)


def run_monitor_once(config_path: str, force_notify: bool, mobile: bool) -> None:
    config = load_config(config_path)
    provider = TencentQuoteProvider(config.monitor)
    conn = connect_db(config.storage.sqlite_path)

    for stock in config.monitor.stocks:
        history = load_recent_quotes(conn, stock.symbol, config.monitor.history_size - 1)
        quote = provider.fetch_quote(stock)
        history.append(quote)
        result = analyze_quotes(history, config.monitor)
        print("=" * 80)
        rendered = format_mobile_signal(result.title, result.message) if mobile else result.message
        if not mobile:
            print(result.title)
        print(rendered)
        quote_id = insert_quote(conn, quote)
        insert_signal(conn, quote_id, quote, result)
        if force_notify or result.should_notify or config.monitor.notification.notify_on_neutral:
            if config.monitor.notification.feishu.enabled:
                payload = format_mobile_signal(result.title, result.message, include_title=False) if mobile else result.message
                deliver_feishu_message(config.monitor.notification.feishu, result.title, payload)


def run_monitor_daemon(config_path: str) -> None:
    config = load_config(config_path)
    runtime = MonitorRuntime(config)
    runtime.serve_forever()


def run_portfolio_report(config_path: str, snapshot_path: str, notify: bool) -> None:
    config = load_config(config_path)
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
    config = load_config(config_path)
    conn = connect_db(config.storage.sqlite_path)
    stats = replay_signal_stats(conn, symbol=symbol, signal_level=level, action=action)
    rendered = format_mobile_replay(stats, symbol=symbol, level=level, action=action)
    print(rendered)
    if notify and config.monitor.notification.feishu.enabled:
        deliver_feishu_message(config.monitor.notification.feishu, "历史回放统计", rendered)


def run_mobile_brief(config_path: str, notify: bool) -> None:
    config = load_config(config_path)
    conn = connect_db(config.storage.sqlite_path)
    rendered = format_mobile_digest(fetch_latest_briefing(conn))
    print(rendered)
    if notify and config.monitor.notification.feishu.enabled:
        deliver_feishu_message(config.monitor.notification.feishu, "AI股票决策简报", rendered)


def run_feishu_bot(config_path: str) -> None:
    config = load_config(config_path)
    serve_feishu_bot(config)


if __name__ == "__main__":
    main()
