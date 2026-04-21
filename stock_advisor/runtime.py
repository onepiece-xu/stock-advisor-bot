from __future__ import annotations

import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from pathlib import Path

from .analysis import analyze_quotes
from .briefing import format_mobile_signal
from .config import AppConfig
from .habit_learning import build_trading_habit_profile
from .market_hours import MARKET_TZ, is_a_share_trading_time, is_auction_period, is_high_volatility_period
from .models import StockQuote
from .logging_utils import get_logger
from .news import fetch_announcements_for_code
from .notify import deliver_feishu_message
from .portfolio import find_holding, load_snapshot as load_portfolio_snapshot
from .providers import EastmoneyMarketSnapshotProvider, EastmoneyMinuteHistoryProvider, TencentQuoteProvider
from .review import already_sent_close_review, build_close_review, mark_close_review_sent, should_send_close_review_now
from .storage import cache_quotes, connect_db, load_recent_quotes, persist_observation
from .trading_plan import detect_trigger_hit, load_snapshot as load_trade_snapshot, load_triggers, render_trade_instruction


logger = get_logger(__name__)


class MonitorRuntime:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.provider = self._build_provider()
        self.history: dict[str, list[StockQuote]] = defaultdict(list)
        self.last_notifications: dict[str, tuple[str, datetime]] = {}
        self.db = connect_db(config.storage.sqlite_path)
        self.trade_triggers = load_triggers(config.trading_plan.path)
        self.market_snapshot = EastmoneyMarketSnapshotProvider(config.monitor)
        self.price_high_marks: dict[str, Decimal] = {}
        self._price_high_marks_date: date | None = None
        self._market_context_cache: tuple[Decimal, dict[str, int], list[dict]] | None = None
        self._market_context_cached_at: datetime | None = None
        self._pre_market_sent_dates: set[date] = set()

    def run_once(self) -> None:
        self._prune_notifications()
        if self.config.monitor.schedule.restrict_to_trading_session and not is_a_share_trading_time():
            self._maybe_send_pre_market_briefing()
            self._maybe_send_close_review()
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] skip: outside A-share trading session")
            return

        today = datetime.now(MARKET_TZ).date()
        if self._price_high_marks_date != today:
            self.price_high_marks.clear()
            self._price_high_marks_date = today

        portfolio_snapshot = self._load_portfolio_snapshot()
        cash_ratio = _compute_cash_ratio(portfolio_snapshot)
        benchmark_history = self._load_benchmark_history()
        trading_habit_profile = build_trading_habit_profile(self.db)
        advance_ratio, rank_map, sector_boards = self._load_market_context()
        volatile_period = is_high_volatility_period()
        for stock in self.config.monitor.stocks:
            bucket = self._load_stock_history(stock)
            if not bucket:
                logger.warning("No history loaded for symbol=%s", stock.symbol)
                continue
            quote = bucket[-1]

            holding = find_holding(portfolio_snapshot, stock.code)
            prev_peak = self.price_high_marks.get(stock.code, quote.current_price)
            if quote.current_price > prev_peak:
                self.price_high_marks[stock.code] = quote.current_price
            result = analyze_quotes(
                bucket,
                self.config.monitor,
                portfolio_holding=holding,
                benchmark_history=benchmark_history,
                trading_habit_profile=trading_habit_profile,
                market_advance_ratio=advance_ratio,
                hot_stock_rank=rank_map.get(stock.code, 0),
                is_volatile_period=volatile_period,
                portfolio_cash_ratio=cash_ratio,
                sector_boards=sector_boards,
                portfolio_position_ratio=_compute_position_ratio(portfolio_snapshot, holding, quote.current_price),
            )
            persist_observation(self.db, quote, result)
            print("=" * 80)
            print(result.title)
            print(result.message)

            stop_loss_msg = self._check_stop_loss(quote, holding)
            if stop_loss_msg:
                self._notify(stock.symbol + ':stop_loss', f"止损预警 {quote.code} {quote.name}", stop_loss_msg)
            else:
                approaching_msg = self._check_stop_loss_approaching(quote, holding)
                if approaching_msg:
                    self._notify(stock.symbol + ':stop_approaching', f"止损临近 {quote.code} {quote.name}", approaching_msg)
            trigger_message = self._build_trigger_message(quote)
            if trigger_message:
                self._notify(stock.symbol + ':trigger', f"{quote.code} {quote.name} 触发交易区间", trigger_message)
            elif self._should_notify(stock.symbol, result, volatile_period):
                self._notify(stock.symbol, result.title, format_mobile_signal(result.title, result.message, include_title=False))

    def serve_forever(self) -> None:
        if self.config.monitor.schedule.run_on_startup:
            self._run_guarded_once("startup")
        if not self.config.monitor.schedule.enabled:
            logger.info("Monitor schedule disabled; exiting after startup pass")
            return
        while True:
            time.sleep(self.config.monitor.schedule.fixed_delay_seconds)
            self._run_guarded_once("loop")

    def _run_guarded_once(self, phase: str) -> None:
        try:
            self.run_once()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Monitor run failed phase=%s error=%s", phase, exc)

    def _load_market_context(self) -> tuple[Decimal, dict[str, int], list[dict]]:
        _cache_ttl = timedelta(minutes=5)
        if (
            self._market_context_cache is not None
            and self._market_context_cached_at is not None
            and datetime.now() - self._market_context_cached_at < _cache_ttl
        ):
            return self._market_context_cache

        try:
            breadth = self.market_snapshot.fetch_market_breadth()
            total = breadth.get("up_count", 0) + breadth.get("flat_count", 0) + breadth.get("down_count", 0)
            advance_ratio = Decimal(str(breadth["up_count"])) / Decimal(str(total)) if total > 0 else Decimal("0")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Market breadth fetch failed error=%s", exc)
            advance_ratio = self._market_context_cache[0] if self._market_context_cache else Decimal("0")
        try:
            top_stocks = self.market_snapshot.fetch_top_stocks(limit=50)
            rank_map = {item["code"]: idx + 1 for idx, item in enumerate(top_stocks)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Top stocks fetch failed error=%s", exc)
            rank_map = self._market_context_cache[1] if self._market_context_cache else {}
        try:
            industry_boards = self.market_snapshot.fetch_sector_boards(kind="industry", limit=5)
            concept_boards = self.market_snapshot.fetch_sector_boards(kind="concept", limit=5)
            sector_boards: list[dict] = industry_boards + concept_boards
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sector boards fetch failed error=%s", exc)
            sector_boards = self._market_context_cache[2] if self._market_context_cache else []
        self._market_context_cache = (advance_ratio, rank_map, sector_boards)
        self._market_context_cached_at = datetime.now()
        return advance_ratio, rank_map, sector_boards

    def _should_notify(self, symbol: str, result, volatile_period: bool = False) -> bool:
        if not self.config.monitor.notification.feishu.enabled:
            return False
        if self.config.monitor.notification.feishu.delivery_mode == "webhook" and not self.config.monitor.notification.feishu.webhook_url:
            return False
        if not (result.should_notify or self.config.monitor.notification.notify_on_neutral):
            return False
        if volatile_period and result.signal_level != "ALERT":
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
        try:
            deliver_feishu_message(self.config.monitor.notification.feishu, title, message)
            self.last_notifications[symbol] = (message, datetime.now())
        except Exception as exc:  # noqa: BLE001
            logger.exception("Notification delivery failed symbol=%s title=%s error=%s", symbol, title, exc)

    def _hydrate_history(self, symbol: str) -> None:
        if self.history[symbol]:
            return
        self.history[symbol].extend(load_recent_quotes(self.db, symbol, self.config.monitor.history_size - 1))

    def _load_stock_history(self, stock) -> list[StockQuote]:
        if self.config.monitor.provider == "eastmoney_minute":
            history = self.provider.fetch_recent_window(stock, self.config.monitor.history_size)
            if history:
                cache_quotes(self.db, history)
            return history

        self._hydrate_history(stock.symbol)
        quote = self.provider.fetch_quote(stock)
        bucket = self.history[stock.symbol]
        bucket.append(quote)
        if len(bucket) > self.config.monitor.history_size:
            del bucket[:-self.config.monitor.history_size]
        return bucket

    def _load_benchmark_history(self) -> list[StockQuote] | None:
        benchmark = self.config.monitor.benchmark
        if benchmark is None:
            return None
        if self.config.monitor.provider == "eastmoney_minute":
            return self.provider.fetch_recent_window(benchmark, self.config.monitor.history_size)
        try:
            return [TencentQuoteProvider(self.config.monitor).fetch_quote(benchmark)]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Benchmark fetch failed symbol=%s error=%s", benchmark.symbol, exc)
            return None

    def _prune_notifications(self) -> None:
        cooldown = max(self.config.monitor.notification.dedup.cooldown_minutes, 1)
        cutoff = datetime.now() - timedelta(minutes=cooldown * 2)
        self.last_notifications = {
            key: value for key, value in self.last_notifications.items() if value[1] > cutoff
        }

    def _compute_effective_stop(self, quote: StockQuote, holding) -> tuple[Decimal, str] | None:
        if holding is None or holding.cost_price <= 0 or holding.quantity <= 0:
            return None
        stop_pct = Decimal(str(self.config.monitor.stop_loss_pct))
        fixed_stop = (holding.cost_price * (1 - stop_pct / 100)).quantize(Decimal("0.001"))
        peak = self.price_high_marks.get(quote.code, quote.current_price)
        float_pct = ((peak - holding.cost_price) / holding.cost_price * 100).quantize(Decimal("0.01"))
        if float_pct >= Decimal("10"):
            trailing = (peak * Decimal("0.97")).quantize(Decimal("0.001"))
            effective_stop = max(fixed_stop, trailing)
            stop_label = f"尾随止损（峰值 {peak}，回撤 3%）"
        elif float_pct >= Decimal("5"):
            effective_stop = max(fixed_stop, holding.cost_price)
            stop_label = "保本止损（浮盈已超 5%，止损线移至成本）"
        else:
            effective_stop = fixed_stop
            stop_label = f"固定止损 -{stop_pct}%"
        return effective_stop, stop_label

    def _check_stop_loss(self, quote: StockQuote, holding) -> str | None:
        computed = self._compute_effective_stop(quote, holding)
        if computed is None:
            return None
        effective_stop, stop_label = computed
        if quote.current_price > effective_stop:
            return None
        pnl_pct = ((quote.current_price - holding.cost_price) / holding.cost_price * 100).quantize(Decimal("0.01"))
        return (
            f"止损预警：{quote.code} {quote.name}\n"
            f"现价 {quote.current_price} 已跌破止损线 {effective_stop}"
            f"（成本 {holding.cost_price}，当前盈亏 {pnl_pct}%，{stop_label}）\n"
            f"请立即检查仓位，考虑止损减仓。"
        )

    def _check_stop_loss_approaching(self, quote: StockQuote, holding) -> str | None:
        computed = self._compute_effective_stop(quote, holding)
        if computed is None:
            return None
        effective_stop, stop_label = computed
        if quote.current_price <= effective_stop:
            return None
        distance_pct = ((quote.current_price - effective_stop) / effective_stop * 100).quantize(Decimal("0.01"))
        if distance_pct > Decimal("2"):
            return None
        pnl_pct = ((quote.current_price - holding.cost_price) / holding.cost_price * 100).quantize(Decimal("0.01"))
        return (
            f"止损临近预警：{quote.code} {quote.name}\n"
            f"现价 {quote.current_price} 距止损线 {effective_stop} 仅 {distance_pct}%"
            f"（成本 {holding.cost_price}，当前盈亏 {pnl_pct}%，{stop_label}）\n"
            f"注意控制仓位，做好止损准备。"
        )

    def _build_trigger_message(self, quote: StockQuote) -> str | None:
        snapshot_path = Path(self.config.storage.sqlite_path).resolve().parent.parent / "portfolio-snapshot.json"
        if not snapshot_path.exists():
            return None
        snapshot = load_trade_snapshot(snapshot_path)
        hit = detect_trigger_hit(quote, snapshot, self.trade_triggers)
        if hit is None:
            return None
        return render_trade_instruction(hit, snapshot)

    def _maybe_send_close_review(self) -> None:
        if not should_send_close_review_now(self.config):
            return
        trade_date = datetime.now(MARKET_TZ).date()
        if already_sent_close_review(self.config, trade_date):
            return
        artifact = build_close_review(self.config, trade_date=trade_date)
        logger.info("Generated close review report path=%s", artifact.saved_path)
        if self.config.monitor.notification.feishu.enabled:
            try:
                deliver_feishu_message(self.config.monitor.notification.feishu, artifact.title, artifact.body)
                mark_close_review_sent(self.config, trade_date)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Close review delivery failed error=%s", exc)
                return
        else:
            mark_close_review_sent(self.config, trade_date)

    def _maybe_send_pre_market_briefing(self) -> None:
        if not is_auction_period():
            return
        today = datetime.now(MARKET_TZ).date()
        if today in self._pre_market_sent_dates:
            return
        lines = [f"【盘前简报】{today.strftime('%Y-%m-%d')} 集合竞价（09:25-09:30）"]
        try:
            industry_boards = self.market_snapshot.fetch_sector_boards(kind="industry", limit=3)
            concept_boards = self.market_snapshot.fetch_sector_boards(kind="concept", limit=3)
            all_boards = industry_boards + concept_boards
            if all_boards:
                lines.append("")
                lines.append("热点板块:")
                for board in all_boards:
                    leader_part = f" 龙头: {board['leader_name']}({board['leader_code']}) {board['leader_change_percent']:+.2f}%" if board.get("leader_name") else ""
                    lines.append(f"- {board['name']} {board.get('change_percent', 0):+.2f}%{leader_part}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pre-market sector boards fetch failed error=%s", exc)
        try:
            ann_lines: list[str] = []
            for stock in self.config.monitor.stocks:
                anns = fetch_announcements_for_code(stock.code, limit=2)
                for ann in anns:
                    ann_lines.append(f"- [{stock.code}] {ann.title} | {ann.published_at}")
            if ann_lines:
                lines.append("")
                lines.append("近期公告:")
                lines.extend(ann_lines)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pre-market announcements fetch failed error=%s", exc)
        lines.append("")
        lines.append("仅供参考，不构成投资建议")
        self._pre_market_sent_dates.add(today)
        if self.config.monitor.notification.feishu.enabled:
            try:
                deliver_feishu_message(
                    self.config.monitor.notification.feishu,
                    f"盘前简报 {today}",
                    "\n".join(lines),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Pre-market briefing delivery failed error=%s", exc)

    def _load_portfolio_snapshot(self):
        snapshot_path = Path(self.config.storage.sqlite_path).resolve().parent.parent / "portfolio-snapshot.json"
        if not snapshot_path.exists():
            return None
        return load_portfolio_snapshot(snapshot_path)

    def _build_provider(self):
        if self.config.monitor.provider == "eastmoney_minute":
            return EastmoneyMinuteHistoryProvider(self.config.monitor)
        return TencentQuoteProvider(self.config.monitor)


def _compute_cash_ratio(snapshot) -> Decimal | None:
    if snapshot is None or snapshot.total_assets <= 0:
        return None
    return (snapshot.cash / snapshot.total_assets).quantize(Decimal("0.0001"))


def _compute_position_ratio(snapshot, holding, current_price: Decimal) -> Decimal | None:
    if snapshot is None or snapshot.total_assets <= 0 or holding is None or holding.quantity <= 0:
        return None
    position_value = Decimal(str(holding.quantity)) * current_price
    return (position_value / snapshot.total_assets).quantize(Decimal("0.0001"))
