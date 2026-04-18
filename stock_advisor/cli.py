from __future__ import annotations

import argparse

from .config import load_config
from .notify import send_feishu_webhook
from .portfolio import build_daily_report, load_previous_snapshot, load_snapshot, save_snapshot
from .providers import TencentQuoteProvider
from .analysis import analyze_quotes
from .runtime import MonitorRuntime


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

    args = parser.parse_args()

    if args.command == "monitor-once":
        run_monitor_once(args.config, args.notify)
    elif args.command == "monitor-daemon":
        run_monitor_daemon(args.config)
    elif args.command == "portfolio-report":
        run_portfolio_report(args.config, args.snapshot, args.notify)


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


if __name__ == "__main__":
    main()
