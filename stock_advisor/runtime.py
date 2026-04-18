from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timedelta

from .analysis import analyze_quotes
from .briefing import format_mobile_signal
from .config import AppConfig
from .direct_notify import write_direct_dm
from .market_hours import is_a_share_trading_time
from .models import StockQuote
from .notify import send_feishu_webhook
from .providers import TencentQuoteProvider
from .storage import connect_db, insert_quote, insert_signal, load_recent_quotes


class MonitorRuntime:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.provider = TencentQuoteProvider(config.monitor)
        self.history: dict[str, list[StockQuote]] = defaultdict(list)
        self.last_notifications: dict[str, tuple[str, datetime]] = {}
        self.db = connect_db(config.storage.sqlite_path)

    def run_once(self) -> None:
        if self.config.monitor.schedule.restrict_to_trading_session and not is_a_share_trading_time():
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] skip: outside A-share trading session")
            return

        for stock in self.config.monitor.stocks:
            self._hydrate_history(stock.symbol)
            quote = self.provider.fetch_quote(stock)
            quote_id = insert_quote(self.db, quote)
            bucket = self.history[stock.symbol]
            bucket.append(quote)
            if len(bucket) > self.config.monitor.history_size:
                del bucket[:-self.config.monitor.history_size]

            result = analyze_quotes(bucket, self.config.monitor)
            insert_signal(self.db, quote_id, quote, result)
            print("=" * 80)
            print(result.title)
            print(result.message)

            if self._should_notify(stock.symbol, result):
                self._notify(stock.symbol, result.title, format_mobile_signal(result.title, result.message, include_title=False))

    def serve_forever(self) -> None:
        if self.config.monitor.schedule.run_on_startup:
            self.run_once()
        while True:
            time.sleep(self.config.monitor.schedule.fixed_delay_seconds)
            self.run_once()

    def _should_notify(self, symbol: str, result) -> bool:
        if not self.config.monitor.notification.feishu.enabled:
            return False
        if self.config.monitor.notification.feishu.delivery_mode == "webhook" and not self.config.monitor.notification.feishu.webhook_url:
            return False
        if not (result.should_notify or self.config.monitor.notification.notify_on_neutral):
            return False
        if not self.config.monitor.notification.dedup.enabled:
            return True

        key = symbol
        summary = "\n".join(result.observations)
        prev = self.last_notifications.get(key)
        if prev is None:
            return True

        previous_summary, previous_time = prev
        cooldown = timedelta(minutes=self.config.monitor.notification.dedup.cooldown_minutes)
        if previous_summary == summary and datetime.now() - previous_time < cooldown:
            return False
        return True

    def _notify(self, symbol: str, title: str, message: str) -> None:
        if self.config.monitor.notification.feishu.delivery_mode == "direct_dm":
            write_direct_dm(title, message)
        else:
            send_feishu_webhook(self.config.monitor.notification.feishu.webhook_url, title, message)
        self.last_notifications[symbol] = ("\n".join(message.splitlines()[-len(message.splitlines()):]), datetime.now())

    def _hydrate_history(self, symbol: str) -> None:
        if self.history[symbol]:
            return
        self.history[symbol].extend(load_recent_quotes(self.db, symbol, self.config.monitor.history_size - 1))
