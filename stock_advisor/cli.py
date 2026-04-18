from __future__ import annotations

import argparse

from .config import load_config
from .notify import send_feishu_webhook
from .portfolio import build_daily_report, load_previous_snapshot, load_snapshot, save_snapshot
from .providers import TencentQuoteProvider
from .analysis import analyze_quotes
from .runtime import MonitorRuntime
from .storage import connect_db, insert_quote, insert_signal, replay_signal_stats


def main() -> None:
    parser = argparse.ArgumentParser(prog="stock-advisor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    monitor_parser = subparsers.add_parser("monitor-once", help="单次获取行情并输出观察报告")
    monitor_parser.add_argument("--config", required=True, help="配置文件路径")
    monitor_parser.add_argument("--notify", action="store_true", help="强制发送 webhook")

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

    args = parser.parse_args()

    if args.command == "monitor-once":
        run_monitor_once(args.config, args.notify)
    elif args.command == "monitor-daemon":
        run_monitor_daemon(args.config)
    elif args.command == "portfolio-report":
        run_portfolio_report(args.config, args.snapshot, args.notify)
    elif args.command == "replay-signals":
        run_replay_signals(args.config, args.symbol, args.level)


def run_monitor_once(config_path: str, force_notify: bool) -> None:
    config = load_config(config_path)
    provider = TencentQuoteProvider(config.monitor)

    for stock in config.monitor.stocks:
        quote = provider.fetch_quote(stock)
        history = [quote]
        result = analyze_quotes(history, config.monitor)
        print("=" * 80)
        print(result.title)
        print(result.message)
        conn = connect_db(config.storage.sqlite_path)
        quote_id = insert_quote(conn, quote)
        insert_signal(conn, quote_id, quote, result)
        if (force_notify or result.should_notify or config.monitor.notification.notify_on_neutral) and config.monitor.notification.feishu.enabled:
            if config.monitor.notification.feishu.webhook_url:
                send_feishu_webhook(config.monitor.notification.feishu.webhook_url, result.title, result.message)


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

    if notify and config.monitor.notification.feishu.enabled and config.monitor.notification.feishu.webhook_url:
        send_feishu_webhook(
            config.monitor.notification.feishu.webhook_url,
            f"收盘持仓建议 {snapshot.trade_date.isoformat()}",
            report,
        )


def run_replay_signals(config_path: str, symbol: str | None, level: str | None) -> None:
    config = load_config(config_path)
    conn = connect_db(config.storage.sqlite_path)
    stats = replay_signal_stats(conn, symbol=symbol, signal_level=level)
    print("【信号回放统计】")
    print(f"生成时间：{stats['generated_at']}")
    print(f"信号样本数：{stats['signal_count']}")
    for horizon, summary in stats["horizons"].items():
        print("")
        print(f"[{horizon} 个周期后]")
        print(f"- 样本数：{summary['samples']}")
        print(f"- 平均收益：{_fmt_stat(summary['avg'])}")
        print(f"- 中位数收益：{_fmt_stat(summary['median'])}")
        print(f"- 胜率：{_fmt_pct(summary['win_rate'])}")
        print(f"- 最差：{_fmt_stat(summary['min'])}")
        print(f"- 最好：{_fmt_stat(summary['max'])}")


def _fmt_stat(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.4f}%"


def _fmt_pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}%"


if __name__ == "__main__":
    main()
