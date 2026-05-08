from __future__ import annotations

import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal


from .analysis import analyze_quotes
from .briefing import format_mobile_signal
from .config import AppConfig
from .habit_learning import build_trading_habit_profile
from .market_hours import MARKET_TZ, is_a_share_trading_time, is_auction_period, is_high_volatility_period
from .models import StockQuote
from .logging_utils import get_logger
from .news import fetch_announcements_for_code
from .notify import deliver_feishu_message
from .portfolio import compute_cash_ratio, compute_position_ratio, find_holding, generate_portfolio_report, load_snapshot as load_portfolio_snapshot
from .portfolio_doc_sync import sync_snapshot_from_doc
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
        self._daily_closes: dict[str, list[Decimal]] = {}
        self._daily_closes_date: date | None = None

    def run_once(self) -> None:
        self._prune_notifications()
        self._sync_portfolio_snapshot_if_needed()
        if self.config.monitor.schedule.restrict_to_trading_session and not is_a_share_trading_time():
            self._maybe_send_pre_market_briefing()
            self._maybe_send_close_review()
            logger.info("skip: outside A-share trading session")
            return

        today = datetime.now(MARKET_TZ).date()
        if self._price_high_marks_date != today:
            self.price_high_marks.clear()
            self._price_high_marks_date = today
        if self._daily_closes_date != today:
            self._daily_closes.clear()
            self._daily_closes_date = today

        portfolio_snapshot = self._load_portfolio_snapshot()
        cash_ratio = compute_cash_ratio(portfolio_snapshot)
        benchmark_history = self._load_benchmark_history()
        trading_habit_profile = build_trading_habit_profile(self.db)
        advance_ratio, rank_map, sector_boards = self._load_market_context()
        volatile_period = is_high_volatility_period()
        pending_notifications: list[tuple[str, str, str]] = []
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
            daily_closes = self._load_daily_closes(stock)
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
                portfolio_position_ratio=compute_position_ratio(portfolio_snapshot, holding, quote.current_price),
                daily_closes=daily_closes,
                portfolio_total_assets=portfolio_snapshot.total_assets if portfolio_snapshot else None,
            )
            persist_observation(self.db, quote, result)
            logger.info("=" * 80)  # type: ignore[arg-type]  # logging format interprets % as placeholder escape
            logger.info(result.title)  # type: ignore[arg-type]
            logger.info(result.message)  # type: ignore[arg-type]

            trigger_message = self._build_trigger_message(quote)
            if trigger_message:
                pending_notifications.append((stock.symbol + ':trigger', f"{quote.code} {quote.name} 触发交易区间", trigger_message))
                continue

            dedup_body = f"{result.decision.action}:{int(result.decision.score) // 10}"
            if self._is_trade_signal(result, holding) and self._dedup_ok(stock.symbol, dedup_body, cooldown_minutes=self.config.monitor.notification.dedup.cooldown_minutes):
                action_label = {"buy": "买入", "reduce": "减仓", "avoid": "清仓"}.get(result.decision.action, result.decision.action)
                pending_notifications.append((stock.symbol, f"{quote.code} {quote.name} {action_label}", format_mobile_signal(result.title, result.message, include_title=False)))

        self._notify_batch(pending_notifications)

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

    def _dedup_ok(self, key: str, message: str, cooldown_minutes: int = 10) -> bool:
        prev = self.last_notifications.get(key)
        if prev is None:
            return True
        prev_msg, prev_time = prev
        return prev_msg != message or datetime.now() - prev_time >= timedelta(minutes=cooldown_minutes)

    def _is_trade_signal(self, result, holding) -> bool:
        if not self.config.monitor.notification.feishu.enabled:
            return False
        if self.config.monitor.notification.feishu.delivery_mode == "webhook" and not self.config.monitor.notification.feishu.webhook_url:
            return False
        action = result.decision.action
        if action == "buy":
            return True
        has_position = holding is not None and holding.quantity > 0
        return action in ("reduce", "avoid") and has_position

    def _notify_batch(self, notifications: list[tuple[str, str, str]]) -> None:
        if not notifications:
            return

        batchable: list[tuple[str, str, str]] = []
        for symbol, title, message in notifications:
            if symbol.endswith(":trigger") or "【盘中交易指令】" in message or "触发交易区间" in title:
                self._notify(symbol, title, message)
            else:
                batchable.append((symbol, title, message))

        if not batchable:
            return
        if len(batchable) == 1:
            symbol, title, message = batchable[0]
            self._notify(symbol, title, message)
            return

        lines = [f"【盘中动作卡】{datetime.now(MARKET_TZ):%H:%M}"]
        dedup_parts: list[str] = []
        for symbol, title, message in batchable:
            body_lines = [line.strip() for line in message.splitlines() if line.strip()]
            action_line = next((line for line in body_lines if line.startswith("动作：") or line.startswith("操作指令：") or line.startswith("直接建议：")), body_lines[0] if body_lines else "")
            size_line = next((line for line in body_lines if line.startswith("执行数量：")), "")
            risk_line = next((line for line in body_lines if line.startswith("风险：")), "")
            stock_name = title.replace(" 行情观察", "")
            action_text = self._strip_label(action_line)
            size_text = self._strip_label(size_line)
            reasons = self._short_risk_reasons(risk_line)
            card_label = self._action_card_label(title, action_text)
            lines.append("")
            lines.append(f"{stock_name}")
            lines.append(f"- 类型：{card_label}")
            if action_text:
                lines.append(f"- 动作：{action_text}")
            if size_text:
                lines.append(f"- 执行：{size_text}")
            if reasons:
                lines.append(f"- 原因：{reasons}")
            dedup_parts.append(f"{symbol}:{action_text}:{size_text}:{reasons}")

        self._notify("batch", "盘中动作卡", "\n".join(lines))
        self.last_notifications["batch"] = ("\n".join(dedup_parts), datetime.now())

    @staticmethod
    def _strip_label(line: str) -> str:
        if not line:
            return ""
        return line.split("：", 1)[1].strip() if "：" in line else line.strip()

    @staticmethod
    def _short_risk_reasons(risk_line: str) -> str:
        text = MonitorRuntime._strip_label(risk_line)
        if not text or "暂无显著风险标记" in text:
            return ""
        parts = [part.strip() for part in text.split("；") if part.strip()]
        return "；".join(parts[:2])

    @staticmethod
    def _action_card_label(title: str, action_text: str) -> str:
        action_text = action_text or title
        if "卖出" in action_text or "减仓" in title:
            return "减仓观察"
        if "买入" in action_text or "买入" in title:
            return "右侧接回"
        if "持有" in action_text or "持有" in title:
            return "继续持有"
        return "收盘后看"

    def _notify(self, symbol: str, title: str, message: str) -> None:
        try:
            deliver_feishu_message(
                self.config.monitor.notification.feishu,
                title,
                message,
                app_id=self.config.feishu_bot.app_id,
                app_secret=self.config.feishu_bot.app_secret,
            )
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

    def _load_daily_closes(self, stock) -> list[Decimal] | None:
        if self.config.monitor.provider != "eastmoney_minute":
            return None
        if stock.symbol not in self._daily_closes:
            try:
                closes = self.provider.fetch_daily_closes(stock, ndays=60)
                self._daily_closes[stock.symbol] = closes
            except Exception as exc:  # noqa: BLE001
                logger.warning("Daily closes fetch failed symbol=%s error=%s", stock.symbol, exc)
                return None
        return self._daily_closes.get(stock.symbol)

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
        if not self.config.snapshot_path.exists():
            return None
        snapshot = load_trade_snapshot(self.config.snapshot_path)
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
                deliver_feishu_message(
                    self.config.monitor.notification.feishu,
                    artifact.title,
                    artifact.body,
                    app_id=self.config.feishu_bot.app_id,
                    app_secret=self.config.feishu_bot.app_secret,
                )
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
                    app_id=self.config.feishu_bot.app_id,
                    app_secret=self.config.feishu_bot.app_secret,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Pre-market briefing delivery failed error=%s", exc)

    def _sync_portfolio_snapshot_if_needed(self) -> None:
        try:
            synced = sync_snapshot_from_doc(snapshot_path=self.config.snapshot_path)
            if synced:
                logger.info("Portfolio snapshot synced from doc")
                self._notify_portfolio_snapshot_update()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Portfolio snapshot sync failed error=%s", exc)

    def _notify_portfolio_snapshot_update(self) -> None:
        if not self.config.snapshot_path.exists():
            logger.warning("Portfolio snapshot update skipped: missing snapshot path=%s", self.config.snapshot_path)
            return
        snapshot, saved_path, report = generate_portfolio_report(self.config.snapshot_path, self.config.portfolio.data_dir)
        logger.info("Portfolio report refreshed from snapshot saved_path=%s trade_date=%s", saved_path, snapshot.trade_date)
        if not self.config.monitor.notification.feishu.enabled:
            return
        deliver_feishu_message(
            self.config.monitor.notification.feishu,
            f"持仓更新建议 {snapshot.trade_date.isoformat()}",
            report,
            app_id=self.config.feishu_bot.app_id,
            app_secret=self.config.feishu_bot.app_secret,
        )

    def _load_portfolio_snapshot(self):
        if not self.config.snapshot_path.exists():
            return None
        return load_portfolio_snapshot(self.config.snapshot_path)

    def _build_provider(self):
        if self.config.monitor.provider == "eastmoney_minute":
            return EastmoneyMinuteHistoryProvider(self.config.monitor)
        return TencentQuoteProvider(self.config.monitor)
