from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path


from .analysis import analyze_quotes
from .briefing import format_mobile_signal
from .config import AppConfig
from .habit_learning import build_trading_habit_profile
from .market_hours import MARKET_TZ, is_a_share_trading_time, is_auction_period, is_high_volatility_period, is_opening_grace_period, next_session_str, seconds_until_next_session
from .models import StockQuote
from .logging_utils import get_logger
from .multi_agent import debate  # v1.55.7: intraday debate
from .instruction_engine import IntradayInstruction, resolve_instruction  # v1.55.21: single decision exit
from .delivery.intraday import render_intraday_action_card  # Phase 4: pure renderer
from .news import fetch_announcements_for_code, fetch_stock_news, filter_new_announcements, format_announcement_line, is_important_announcement
from .notify import deliver_feishu_message
from .outbox import flush_outbox, check_stale
from .portfolio import compute_cash_ratio, compute_position_ratio, find_holding, generate_portfolio_report, load_snapshot as load_portfolio_snapshot
from .portfolio_doc_sync import sync_snapshot_from_doc
from .providers import EastmoneyMarketSnapshotProvider, EastmoneyMinuteHistoryProvider, SinaMinuteHistoryProvider, TencentQuoteProvider
from .opportunity_scanner import scan as scan_opportunities, suggest_position, build_exit_plan, can_afford_candidate
from .review import already_sent_close_review, build_close_review, find_latest_plan_record, mark_close_review_sent, should_send_close_review_now
from .stop_loss import compute_effective_stop
from .storage import cache_quotes, connect_db, load_recent_quotes, persist_observation
from .trading_plan import build_risk_context, detect_trigger_hit, load_snapshot as load_trade_snapshot, load_triggers, render_trade_instruction
from .trade_journal import TradeJournal
import hashlib


logger = get_logger(__name__)


class MonitorRuntime:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.provider = self._build_provider()
        self.sina_provider = SinaMinuteHistoryProvider(config.monitor)  # fallback when eastmoney blocked
        self.tencent_provider = TencentQuoteProvider(config.monitor)  # last-resort fallback
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
        self._last_news_check: datetime | None = None
        # Auto trade logging via snapshot diff
        self._last_snapshot_hash: str = ""
        self._trade_journal = TradeJournal(Path(config.portfolio.data_dir) / "trade_journal")
        # Signal reversal detection: store last pushed action per stock
        self._signal_states: dict[str, str] = {}
        self._signal_state_path = Path(config.portfolio.data_dir) / "signal_history.json"
        self._load_signal_states()
        # v1.55.7: Intraday multi-agent debate cache
        self._debate_cache: dict[str, tuple] = {}       # code -> (decision, timestamp)
        self._debate_attempts: dict[str, datetime] = {}  # code -> last attempt time
        self._debate_interval = 600  # 10 min between debates per stock
        # Intraday opportunity scan removed per push convergence plan (v1.55.22)

    def run_once(self) -> None:
        self._prune_notifications()
        self._check_bridge_health()  # Alert if cron bridge stalled
        self._sync_portfolio_snapshot_if_needed()
        # Pre-market briefing must be checked BEFORE the trading-session guard,
        # because auction period (9:25-9:30) IS trading time.  Previously it was
        # only called when NOT trading, so it could never fire.
        self._maybe_send_pre_market_briefing()
        if self.config.monitor.schedule.restrict_to_trading_session and not is_a_share_trading_time():
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
        self._detect_and_log_trades(portfolio_snapshot)
        self._adjust_for_drawdown(portfolio_snapshot)
        cash_ratio = compute_cash_ratio(portfolio_snapshot)
        benchmark_history = self._load_benchmark_history()
        trading_habit_profile = build_trading_habit_profile(self.db)
        advance_ratio, rank_map, sector_boards = self._load_market_context()
        volatile_period = is_high_volatility_period()
        pending_notifications: list[tuple[str, str, str]] = []

        # Pre-fetch Tencent batch quotes as fallback when eastmoney minute API is blocked.
        # Cache to DB so per-stock fallback can read from DB.
        try:
            tencent_quotes = self.tencent_provider.fetch_quotes_batch(self.config.monitor.stocks)
            if tencent_quotes:
                cache_quotes(self.db, tencent_quotes)
        except Exception as exc:
            logger.warning("Tencent batch poll failed: %s", exc)

        for stock in self.config.monitor.stocks:
            bucket = self._load_stock_history(stock)
            if not bucket:
                logger.warning("No history loaded for symbol=%s", stock.symbol)
                continue
            quote = bucket[-1]

            holding = find_holding(portfolio_snapshot, stock.code)
            if holding is not None and holding.quantity <= 0:
                continue
            prev_peak = self.price_high_marks.get(stock.code, quote.current_price)
            if quote.current_price > prev_peak:
                self.price_high_marks[stock.code] = quote.current_price
            daily_closes, daily_volumes = self._load_daily_klines(stock)
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
                daily_volumes=daily_volumes,
                portfolio_total_assets=portfolio_snapshot.total_assets if portfolio_snapshot else None,
                peak_price=self.price_high_marks.get(stock.code),
            )
            persist_observation(self.db, quote, result)
            logger.info("=" * 80)  # type: ignore[arg-type]  # logging format interprets % as placeholder escape
            logger.info(result.title)  # type: ignore[arg-type]
            logger.info(result.message)  # type: ignore[arg-type]

            # ── Phase 3: Instruction Engine — single decision per stock ──
            # Collect scoring + debate + trigger into one instruction resolution
            instr = self._resolve_instruction(
                code=stock.code,
                name=quote.name,
                current_price=quote.current_price,
                score_result=result,
                holding=holding,
                quote=quote,
                advance_ratio=advance_ratio,
            )

            # Trigger notifications go through instruction engine now
            if instr and instr.action in ("buy", "sell"):
                dedup_body = f"{instr.action}:{instr.priority}"
                if self._dedup_ok(stock.symbol, dedup_body, cooldown_minutes=self.config.monitor.notification.dedup.cooldown_minutes):
                    action_label = "买入" if instr.action == "buy" else "卖出"
                    pending_notifications.append((
                        stock.symbol,
                        f"{stock.code} {quote.name} {action_label}",
                        format_mobile_signal(
                            f"{quote.name} → {action_label}（{instr.source}）",
                            instr.reason,
                            include_title=False,
                        ),
                        dedup_body,
                    ))
                    self._update_signal_state(stock.code, instr.action)

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
            # After close review is done, sleep until next trading session
            try:
                if not is_a_share_trading_time() and already_sent_close_review(
                    self.config, datetime.now(MARKET_TZ).date()
                ):
                    sleep_sec = seconds_until_next_session()
                    logger.info(
                        "Close review sent, sleeping until next session (%s seconds)",
                        sleep_sec,
                    )
                    time.sleep(sleep_sec)
            except Exception as exc:
                logger.warning("休眠计算失败，继续轮询: %s", exc)

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
        elapsed = (datetime.now() - prev[1]).total_seconds()
        return elapsed > (cooldown_minutes * 60)

    # ── Signal reversal detection ──

    REVERSAL_PAIRS: dict[tuple[str, str], str] = {
        # (yesterday, today) → action to take
        ("buy", "reduce"): "suppress",   # Don't sell right after telling to buy
        ("reduce", "buy"): "suppress",   # Don't buy right after telling to sell
        ("avoid", "buy"): "suppress",    # Don't buy if yesterday said avoid
        ("avoid", "reduce"): "suppress", # Don't sell if yesterday said avoid (already avoided)
    }

    def _load_signal_states(self) -> None:
        """Load previous signal states from disk."""
        try:
            if self._signal_state_path.exists():
                self._signal_states = json.loads(self._signal_state_path.read_text())
        except Exception as exc:
            logger.warning("Failed to load signal states: %s", exc)
            self._signal_states = {}

    def _save_signal_states(self) -> None:
        """Persist signal states to disk."""
        try:
            self._signal_state_path.parent.mkdir(parents=True, exist_ok=True)
            self._signal_state_path.write_text(json.dumps(self._signal_states, ensure_ascii=False))
        except Exception as exc:
            logger.warning("Failed to save signal states: %s", exc)

    def _check_signal_reversal(self, code: str, action: str) -> bool:
        """Return True if this signal is a reversal and should be suppressed.

        A reversal is when today's action contradicts yesterday's action.
        E.g. yesterday=buy, today=reduce → suppress (noise, not a real change).
        """
        prev = self._signal_states.get(code)
        if prev is None:
            return False  # No history, allow
        verdict = self.REVERSAL_PAIRS.get((prev, action))
        if verdict == "suppress":
            logger.info(
                "Signal reversal suppressed: %s %s→%s (yesterday→today)",
                code, prev, action,
            )
            return True
        return False

    def _update_signal_state(self, code: str, action: str) -> None:
        """Update signal state after a push."""
        self._signal_states[code] = action
        self._save_signal_states()

    def _resolve_instruction(
        self, *, code: str, name: str, current_price: Decimal,
        score_result, holding, quote, advance_ratio: Decimal,
    ) -> IntradayInstruction | None:
        """Phase 3: Resolve scoring + debate + trigger into ONE instruction per stock."""
        score_action = score_result.decision.action
        score_confidence = score_result.decision.confidence

        # ── Filter: low-confidence neutral zone ──
        score_val = score_result.decision.score
        if score_confidence == "low" and Decimal("45") <= score_val <= Decimal("55") and score_action in ("hold", "avoid"):
            return None

        # ── Signal reversal guard ──
        if code and self._check_signal_reversal(code, score_action):
            return None

        # ── Opening grace period: mute reduce/avoid ──
        has_position = holding is not None and holding.quantity > 0
        if score_action in ("reduce", "avoid") and has_position and is_opening_grace_period():
            return None

        # ── Debate ──
        debate_action = None
        debate_confidence = 0.0
        if score_action in ("buy", "reduce"):
            debate_decision = self._intraday_debate(quote, holding, advance_ratio)
            if debate_decision and debate_decision.action != score_action:
                logger.info(
                    "🔄 Debate override: %s scoring=%s→debate=%s (confidence=%.0f%%)",
                    name, score_action, debate_decision.action,
                    debate_decision.confidence * 100,
                )
                debate_action = debate_decision.action
                debate_confidence = debate_decision.confidence

        # ── Trigger hit ──
        trigger_action = None
        trigger_quantity = 0
        trigger_message = self._build_trigger_message(quote)
        if trigger_message and score_action != "avoid":
            from .trading_plan import detect_trigger_hit, load_snapshot as load_trade_snapshot
            try:
                snapshot = load_trade_snapshot(self.config.snapshot_path)
                trigger_hit = detect_trigger_hit(quote, snapshot, self.trade_triggers)
                if trigger_hit:
                    trigger_action = trigger_hit.trigger.action
                    trigger_quantity = trigger_hit.quantity
            except Exception as exc:
                logger.warning("Trigger resolution failed: %s", exc)

        # ── Holdings context ──
        holding_quantity = holding.quantity if holding else 0
        holding_pnl_pct = 0.0
        if holding and holding.cost_price > 0:
            holding_pnl_pct = float((holding.current_price - holding.cost_price) / holding.cost_price * 100)

        return resolve_instruction(
            code=code,
            name=name,
            current_price=current_price,
            score_action=score_action,
            score_confidence=score_confidence,
            debate_action=debate_action,
            debate_confidence=debate_confidence,
            trigger_hit_action=trigger_action,
            trigger_hit_quantity=trigger_quantity,
            holding_quantity=holding_quantity,
            holding_pnl_pct=holding_pnl_pct,
            max_single_position_pct=self.config.monitor.risk_controls.max_single_position_pct,
        )

    def _intraday_debate(self, quote, holding, advance_ratio):
        """Run multi-agent debate intraday for a stock with a trade signal.

        Cached for 10 min per stock to avoid excessive API calls.
        Returns debate decision or None if debate skipped/failed.
        """
        code = quote.code
        now = datetime.now()

        # Check cache — use recent debate result if available
        if code in self._debate_cache:
            decision, cached_at = self._debate_cache[code]
            if (now - cached_at).total_seconds() < self._debate_interval:
                return decision

        # Check attempt cooldown — don't spam debate calls
        last_attempt = self._debate_attempts.get(code)
        if last_attempt and (now - last_attempt).total_seconds() < self._debate_interval:
            return None

        self._debate_attempts[code] = now

        # Prepare context
        holding_info = ""
        if holding and holding.quantity > 0:
            pnl = float((quote.current_price - holding.cost_price) / holding.cost_price * 100)
            holding_info = f"持仓{holding.quantity}股，成本{float(holding.cost_price):.2f}，盈亏{pnl:+.1f}%"

        market_context = f"涨跌比{float(advance_ratio):.2f}" if advance_ratio else ""

        try:
            decision = debate(
                symbol=code,
                name=quote.name,
                current_price=quote.current_price,
                holding_info=holding_info,
                market_context=market_context,
                timeout_per_agent=15,
            )
            if decision:
                self._debate_cache[code] = (decision, now)
                logger.info(
                    "🧠 Debate: %s → %s (vote=%s confidence=%.0f%%)",
                    quote.name, decision.action, decision.vote_summary,
                    decision.confidence * 100,
                )
                return decision
        except Exception as exc:
            logger.warning("Intraday debate failed for %s: %s", quote.name, exc)

        return None

    def _notify_batch(self, notifications: list[tuple[str, str, str]]) -> None:
        if not notifications:
            return

        batchable: list[tuple[str, str, str, str]] = []
        for item in notifications:
            symbol, title, message = item[0], item[1], item[2]
            dedup_body = item[3] if len(item) > 3 else ""
            if symbol.endswith(":trigger") or "【盘中交易指令】" in message or "触发交易区间" in title:
                self._notify(symbol, title, message)
            else:
                batchable.append((symbol, title, message, dedup_body))

        if not batchable:
            return
        if len(batchable) == 1:
            symbol, title, message, dedup_body = batchable[0]
            self._notify(symbol, title, message)
            return

        # ── Phase 4: Use delivery renderer for action card ──
        instructions = []
        dedup_parts: list[str] = []
        for symbol, title, message, dedup_body in batchable:
            body_lines = [line.strip() for line in message.splitlines() if line.strip()]
            action_line = next((line for line in body_lines if line.startswith("动作：") or line.startswith("操作指令：") or line.startswith("直接建议：")), body_lines[0] if body_lines else "")
            size_line = next((line for line in body_lines if line.startswith("执行数量：")), "")
            risk_line = next((line for line in body_lines if line.startswith("风险：")), "")
            stock_name = title.replace(" 行情观察", "")
            action_text = self._strip_label(action_line)
            size_text = self._strip_label(size_line)
            reasons = self._short_risk_reasons(risk_line)
            card_label = self._action_card_label(title, action_text)
            instructions.append({
                "name": stock_name,
                "action_text": action_text,
                "size_text": size_text,
                "reasons": reasons,
                "card_label": card_label,
            })
            dedup_parts.append(f"{symbol}:{action_text}:{size_text}:{reasons}")

        card_text = render_intraday_action_card(instructions, timestamp=datetime.now(MARKET_TZ))
        self._notify("batch", "盘中动作卡", card_text)
        self.last_notifications["batch"] = ("\n".join(dedup_parts), datetime.now())
        # ── Per-symbol dedup: update individual stock entries so _dedup_ok
        #     can enforce per-stock cooldown in subsequent daemon cycles ──
        now = datetime.now()
        for symbol, _title, _msg, dedup_body in batchable:
            if dedup_body:
                self.last_notifications[symbol] = (dedup_body, now)

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
            # eastmoney blocked — try Sina minute API
            logger.warning(
                "eastmoney returned empty for %s, trying sina fallback", stock.symbol
            )
            history = self.sina_provider.fetch_recent_window(stock, self.config.monitor.history_size)
            if history:
                cache_quotes(self.db, history)
                return history
            # Both blocked — fallback to Tencent real-time + DB history
            logger.warning(
                "sina also failed for %s, falling back to tencent+DB", stock.symbol
            )
            tencent_quote = self._poll_tencent_single(stock)
            if tencent_quote:
                cache_quotes(self.db, [tencent_quote])
            db_history = load_recent_quotes(self.db, stock.symbol, self.config.monitor.history_size)
            if tencent_quote and (not db_history or db_history[-1].quote_time < tencent_quote.quote_time):
                db_history.append(tencent_quote)
            return db_history

        self._hydrate_history(stock.symbol)
        quote = self.provider.fetch_quote(stock)
        bucket = self.history[stock.symbol]
        bucket.append(quote)
        if len(bucket) > self.config.monitor.history_size:
            del bucket[:-self.config.monitor.history_size]
        return bucket

    def _poll_tencent_single(self, stock) -> StockQuote | None:
        """Fetch a single quote via Tencent API. Used as fallback."""
        try:
            return self.tencent_provider.fetch_quote(stock)
        except Exception as exc:
            logger.warning("Tencent fallback failed for %s: %s", stock.symbol, exc)
            return None

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

    def _load_daily_klines(self, stock) -> tuple[list[Decimal] | None, list[Decimal] | None]:
        """Returns (daily_closes, daily_volumes) for the stock, cached per day."""
        if self.config.monitor.provider != "eastmoney_minute":
            return None, None
        cache_key = stock.symbol
        if cache_key not in self._daily_closes:
            try:
                closes, volumes = self.provider.fetch_daily_klines(stock, ndays=60)
                self._daily_closes[cache_key] = closes
                self._daily_volumes = getattr(self, '_daily_volumes', {})
                self._daily_volumes[cache_key] = volumes
            except Exception as exc:
                logger.warning("Daily klines fetch failed symbol=%s error=%s", stock.symbol, exc)
                return None, None
        daily_volumes_dict = getattr(self, '_daily_volumes', {})
        return self._daily_closes.get(cache_key), daily_volumes_dict.get(cache_key)

    def _prune_notifications(self) -> None:
        cooldown = max(self.config.monitor.notification.dedup.cooldown_minutes, 1)
        cutoff = datetime.now() - timedelta(minutes=cooldown * 2)
        self.last_notifications = {
            key: value for key, value in self.last_notifications.items() if value[1] > cutoff
        }

    def _check_bridge_health(self) -> None:
        """Alert if notifications have been stuck in the outbox too long."""
        try:
            stale = check_stale(max_age_minutes=5)
            if stale:
                logger.warning(
                    "Bridge health: %d notifications stuck >5min in outbox. "
                    "Cron bridge may be stalled.",
                    len(stale),
                )
        except Exception as exc:
            logger.warning("Health check stale trigger cleanup failed: %s", exc)  # Don't let health check crash the daemon

    def _compute_effective_stop(self, quote: StockQuote, holding) -> tuple[Decimal, str, str] | None:
        if holding is None or holding.cost_price <= 0 or holding.quantity <= 0:
            return None
        peak = self.price_high_marks.get(quote.code, quote.current_price)
        return compute_effective_stop(
            cost_price=holding.cost_price,
            current_price=quote.current_price,
            peak_price=peak,
            stop_loss_pct=self.config.monitor.stop_loss_pct,
        )

    def _check_stop_loss(self, quote: StockQuote, holding) -> str | None:
        computed = self._compute_effective_stop(quote, holding)
        if computed is None:
            return None
        effective_stop, stop_label, _distance = computed
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
        effective_stop, stop_label, distance_pct = computed
        if distance_pct <= 0 or distance_pct > Decimal("2"):
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
        risk = build_risk_context(quote)
        hit = detect_trigger_hit(quote, snapshot, self.trade_triggers, risk=risk)
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
                # Outbox backlog alert: warn if items accumulated without being pushed
                outbox_path = Path("data/outbox.jsonl")
                if outbox_path.exists() and outbox_path.stat().st_size > 0:
                    outbox_lines = outbox_path.read_text().strip().split("\n")
                    stale_count = len(outbox_lines)
                    if stale_count > 3:
                        alert = f"⚠️ Outbox 积压 {stale_count} 条，cron bridge 可能卡死，请检查 bridge 进程"
                        logger.warning(alert)
                        # 主动推飞书通知，不依赖 cron bridge 自身
                        try:
                            deliver_feishu_message(
                                self.config.monitor.notification.feishu,
                                "⚠️ Outbox 积压告警",
                                alert,
                                app_id=self.config.feishu_bot.app_id,
                                app_secret=self.config.feishu_bot.app_secret,
                            )
                        except Exception:
                            pass
                flush_outbox()  # 双保险：不等 cron，立即推送
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
        lines = [f"📊 {today.strftime('%m/%d')} 盘前"]
        snapshot = self._load_portfolio_snapshot()

        # ── 1. 大盘风向 ──
        try:
            benchmark = self.config.monitor.benchmark
            if benchmark:
                tencent = TencentQuoteProvider(self.config.monitor)
                bq = tencent.fetch_quote(benchmark)
                direction = "偏强" if bq.change_percent >= 0 else "偏弱"
                lines.append(f"\n大盘风向：{bq.name} {bq.current_price}（{bq.change_percent:+.2f}%）{direction}")
        except Exception as exc:
            logger.warning("Pre-market benchmark fetch failed error=%s", exc)

        # ── 1.5 昨日计划对照 ──
        try:
            plan = find_latest_plan_record(self.config.review.data_dir)
            if plan and plan.get("holdings"):
                plan_date = plan.get("plan_date", "?")
                lines.append(f"\n【昨日计划对照】{plan_date}")
                # Build a quick lookup: code -> current auction price
                current_prices: dict[str, Decimal] = {}
                if snapshot:
                    tencent = TencentQuoteProvider(self.config.monitor)
                    for holding in snapshot.holdings:
                        if holding.quantity <= 0:
                            continue
                        try:
                            stock_ref = next(
                                (s for s in self.config.monitor.stocks if s.code == holding.code), None
                            )
                            if stock_ref:
                                q = tencent.fetch_quote(stock_ref)
                                current_prices[holding.code] = q.current_price
                        except Exception as exc:
                            logger.warning("Failed to fetch quote for %s: %s", holding.code, exc)
                for h in plan["holdings"]:
                    code = h.get("code", "")
                    name = h.get("name", "")
                    planned_action = h.get("planned_action", "hold")
                    yesterday_price = h.get("current_price", 0)
                    today_price = current_prices.get(code)
                    planned_pnl = h.get("pnl_pct", 0)
                    if today_price and yesterday_price > 0:
                        overnight_chg = (float(today_price) - yesterday_price) / yesterday_price * 100
                        chg_str = f"{overnight_chg:+.2f}%"
                    else:
                        chg_str = "N/A"
                    # Determine if action was followed through
                    price_str = f"{today_price}" if today_price else "?"
                    # Map planned action to status emoji
                    action_emoji = {"buy": "🟢", "hold": "🟡", "reduce": "🔴", "avoid": "⛔"}.get(planned_action, "⚪")
                    lines.append(
                        f"  {action_emoji} {name}({code})："
                        f"昨收 {yesterday_price:.2f} | 今竞价 {price_str}（{chg_str}）"
                        f" | 昨建议 {planned_action}"
                    )
                    # Show trigger check for this stock
                    for t in plan.get("triggers", []):
                        if t.get("code") == code and not t.get("is_orphan"):
                            t_min = t.get("price_min", 0)
                            t_max = t.get("price_max", 0)
                            if today_price and t_min and t_max:
                                if t_min <= float(today_price) <= t_max:
                                    lines.append(f"    ⚡ 昨触发单区间 {t_min}-{t_max}，今竞价 {today_price} 已进入触发区！")
                                elif abs(float(today_price) - t_min) <= abs(t_min * 0.05):
                                    lines.append(f"    👀 昨触发单区间 {t_min}-{t_max}，今竞价 {today_price} 接近触发区")
        except Exception as exc:
            logger.warning("Plan comparison section failed error=%s", exc)

        # ── 2. 持仓个股竞价数据 + 今日关键价位 ──
        if snapshot:
            lines.append("\n【持仓竞价】")
            tencent = TencentQuoteProvider(self.config.monitor)
            stop_pct = self.config.monitor.stop_loss_pct
            for holding in snapshot.holdings:
                if holding.quantity <= 0:
                    continue
                try:
                    stock_ref = next(
                        (s for s in self.config.monitor.stocks if s.code == holding.code), None
                    )
                    if stock_ref is None:
                        continue
                    q = tencent.fetch_quote(stock_ref)
                    pnl = ((q.current_price - holding.cost_price) / holding.cost_price * 100) if holding.cost_price > 0 else 0
                    pnl_str = f"{pnl:+.1f}%"
                    eff_stop, stop_label, _ = compute_effective_stop(
                        cost_price=holding.cost_price,
                        current_price=q.current_price,
                        stop_loss_pct=stop_pct,
                    )
                    lines.append(
                        f"- {q.name} {q.current_price}（{q.change_percent:+.2f}%）"
                        f" | 盈亏{pnl_str} | {stop_label}{eff_stop}"
                    )
                except Exception as exc:
                    logger.warning("Pre-market quote fetch failed code=%s error=%s", holding.code, exc)

        # ── 3. 今日触发单状态 ──
        try:
            triggers = self.trade_triggers if self.trade_triggers else []
            if triggers and snapshot:
                active_codes = {h.code for h in snapshot.holdings if h.quantity > 0}
                active_triggers = [t for t in triggers if t.code in active_codes]
                if active_triggers:
                    lines.append("\n【今日触发单】")
                    for t in active_triggers:
                        lines.append(
                            f"- {t.name}：{t.action} {t.quantity}股 "
                            f"@ {t.price_min}-{t.price_max}"
                            f"（回落 {t.fallback_price}）"
                        )
        except Exception as exc:
            logger.warning("Trigger plan rendering failed: %s", exc)

        # ── 3.5 昨日主力资金 ──
        try:
            from .chrome_scraper import get_multi_fund_flow
            if snapshot:
                codes = [h.code for h in snapshot.holdings if h.quantity > 0]
                ff_data = get_multi_fund_flow(codes)
                if ff_data:
                    lines.append("\n【昨日主力资金】")
                    for code in codes:
                        ff = ff_data.get(code)
                        if ff:
                            d = "🟢流入" if ff["main_net_yi"] > 0 else ("🔴流出" if ff["main_net_yi"] < 0 else "⚪持平")
                            lines.append(
                                f"  {code} {ff['name']} {d} {abs(ff['main_net_yi']):.2f}亿"
                            )
        except Exception as exc:
            logger.warning("Fund flow display failed: %s", exc)

        # ── 4. 板块轮动热力图 ── (REMOVED — office worker doesn't need sector heatmap)
        # ── 5. 近期公告 ──
        try:
            ann_lines: list[str] = []
            for stock in self.config.monitor.stocks:
                anns = fetch_announcements_for_code(stock.code, limit=3)
                anns = filter_new_announcements(anns)
                for ann in anns:
                    ann_lines.append(f"[{stock.code}] {format_announcement_line(ann)}")
            if ann_lines:
                lines.append("\n【近期公告】")
                lines.extend(ann_lines)
        except Exception as exc:
            logger.warning("Pre-market announcements fetch failed error=%s", exc)

        # ── 6. 现金仓位提示 ＋ 今日速判 ──
        if snapshot and snapshot.total_assets > 0:
            cash_pct = (snapshot.cash / snapshot.total_assets * 100)
            lines.append(f"\n【账户总览】")
            lines.append(f"总资产 {snapshot.total_assets:.0f} | 现金 {snapshot.cash:.0f}（{cash_pct:.0f}%）")
            if cash_pct > 60:
                # ── 现金部署评估 ──
                can_deploy = True
                deploy_blockers: list[str] = []
                benchmark_ok = True
                try:
                    benchmark = self.config.monitor.benchmark
                    if benchmark:
                        tencent = TencentQuoteProvider(self.config.monitor)
                        bq = tencent.fetch_quote(benchmark)
                        if bq.change_percent <= -1.5:
                            can_deploy = False
                            benchmark_ok = False
                            deploy_blockers.append(f"大盘 {bq.change_percent:+.2f}% 偏弱，不急于入场")
                        elif bq.change_percent <= -0.5:
                            deploy_blockers.append(f"大盘 {bq.change_percent:+.2f}% 略弱，仓位不宜过重")
                    snapshot_ratio = (snapshot.cash / snapshot.total_assets * 100)
                except Exception as exc:
                    logger.warning("Market breadth data failed: %s", exc)
                    deploy_blockers.append("大盘数据获取失败")
                    benchmark_ok = False

                lines.append(f"\\n【现金部署评估】现金 {snapshot.cash:.0f}（{cash_pct:.0f}%）")
                if can_deploy:
                    lines.append("✅ 大盘环境尚可，现金充裕，可关注今日入场机会")
                    lines.append("  首选：现有浮盈持仓加仓 > 已清仓旧标的接回 > 全新标的试仓")
                    lines.append("  纪律：单次不超过总资产 10%，涨超 3% 不追，等回踩 MA10")
                else:
                    lines.append(f"🚫 暂不建议入场：{'；'.join(deploy_blockers)}")
                if deploy_blockers:
                    lines.append(f"  {'；'.join(deploy_blockers)}")

        # ── 7. 今日速判（一句话操作建议）──
        quick_verdicts: list[str] = []
        if snapshot:
            try:
                for holding in snapshot.holdings:
                    if holding.quantity <= 0:
                        continue
                    pnl = ((holding.current_price - holding.cost_price) / holding.cost_price * 100) if holding.cost_price > 0 else 0
                    # Check if price is near any trigger range
                    trigger_near = False
                    for t in self.trade_triggers.values():
                        if t.code == holding.code:
                            dist = min(
                                abs(holding.current_price - t.price_min),
                                abs(holding.current_price - t.price_max),
                            )
                            if dist <= (t.price_max - t.price_min) * Decimal("2"):
                                trigger_near = True
                            break
                    if pnl <= -30:
                        verdict = "❌ 深套，只减不补"
                    elif trigger_near:
                        verdict = "⚡ 接近触发单，关注"
                    elif pnl >= 8:
                        verdict = "🟢 浮盈充足，可持有或止盈"
                    elif pnl >= 0:
                        verdict = "🟡 小幅浮盈，持有观望"
                    else:
                        plan = build_exit_plan(holding, max_single_position_pct=self.config.monitor.risk_controls.max_single_position_pct)
                        verdict = f"🟡 {plan.action}，目标{plan.target_price}"
                    quick_verdicts.append(f"- {holding.name}：{verdict}")
                if quick_verdicts:
                    lines.append("\n【今日速判】")
                    lines.extend(quick_verdicts)
                exit_lines: list[str] = []
                for holding in snapshot.holdings:
                    if holding.quantity <= 0:
                        continue
                    plan = build_exit_plan(holding, max_single_position_pct=self.config.monitor.risk_controls.max_single_position_pct)
                    if plan.quantity <= 0:
                        continue
                    exit_lines.append(
                        f"- {holding.name}：先看 {plan.target_price} 附近{plan.action}{plan.quantity}股；若回落到 {plan.fallback_price} 再执行，{plan.reason}"
                    )
                if exit_lines:
                    lines.append("\n【持仓卖点计划】")
                    lines.extend(exit_lines)
            except Exception as exc:
                logger.warning("Exit plan generation failed: %s", exc)

        # ── 8. LLM 决策解读（AI 浓缩）──
        try:
            from .llm_analyst import generate_briefing_verdict
            llm_data: list[dict] = []
            if snapshot:
                for h in snapshot.holdings:
                    if h.quantity <= 0:
                        continue
                    eff_stop, stop_label, _ = compute_effective_stop(
                        cost_price=h.cost_price,
                        current_price=h.current_price,
                        stop_loss_pct=self.config.monitor.stop_loss_pct,
                    )
                    trigger_note = ""
                    for t in self.trade_triggers.values():
                        if t.code == h.code:
                            trigger_note = f"触发单：{t.action}@{t.price_min}-{t.price_max}"
                            break
                    llm_data.append({
                        "name": h.name, "code": h.code, "quantity": h.quantity,
                        "cost_price": float(h.cost_price), "current_price": float(h.current_price),
                        "pnl_pct": float((h.current_price - h.cost_price) / h.cost_price * 100) if h.cost_price > 0 else 0,
                        "stop_price": str(eff_stop), "trigger_note": trigger_note,
                    })
            if llm_data:
                cash_ratio = float(snapshot.cash / snapshot.total_assets * 100) if snapshot and snapshot.total_assets > 0 else 0
                verdict = generate_briefing_verdict(
                    llm_data, market_wind="", cash_pct=cash_ratio, today=today.isoformat(),
                )
                if verdict:
                    lines.append("\n【AI 决策解读】")
                    lines.append(verdict)
        except Exception as exc:
            logger.warning("AI decision generation failed: %s", exc)

        # ── 9. 板块强度（当日最强/最弱板块）──
        try:
            from .sector_strength import fetch_sector_boards, format_sector_report
            sectors = fetch_sector_boards(top_n=60)
            if sectors:
                holdings_list = [
                    {"symbol": h.code if hasattr(h, 'code') else h.symbol, "name": h.name}
                    for h in (snapshot.holdings if snapshot else [])
                ]
                sector_text = format_sector_report(sectors, holdings_list)
                if sector_text:
                    lines.append(f"\n{sector_text}")
        except Exception as exc:
            logger.warning("Sector text generation failed: %s", exc)

        # ── 10. 主动机会扫描（排除当前持仓）──
        try:
            exclude_codes = {h.code for h in snapshot.holdings if h.quantity > 0} if snapshot else set()
            candidates = scan_opportunities(
                config_path="config.yaml",
                top_n=3,
                exclude_codes=exclude_codes,
                max_change_pct=5.0,
            )
            if candidates:
                candidates = [c for c in candidates if can_afford_candidate(self.config, c.current_price, c.composite_score)]
            if candidates:
                lines.append("\n【今日主动机会】")
                for idx, cand in enumerate(candidates, 1):
                    stop_price = (cand.current_price * Decimal("0.93")).quantize(Decimal("0.01"))
                    position = suggest_position(self.config, cand.current_price, cand.composite_score)
                    position_text = f"{position.label}{position.quantity}股" if position.quantity > 0 else "现金不足，先观察"
                    reasons = " / ".join(flag.lstrip("🟢🟡🔴🟠📏💪🔔📊📈🔥⚠️ ") for flag in cand.flags[:2])
                    line = (
                        f"- #{idx} {cand.name}({cand.code}) {cand.current_price}"
                        f" | 分{cand.composite_score} | {position_text} | 止损{stop_price}"
                    )
                    if reasons:
                        line += f" | {reasons}"
                    lines.append(line)
        except Exception as exc:
            logger.warning("Pre-market opportunity scan failed error=%s", exc)

        # Save briefing data for status command
        try:
            _save_pre_market_state(Path("data"), today, lines, snapshot)
        except Exception as exc:
            logger.warning("Failed to save pre-market state: %s", exc)

        lines.append(f"\n下次开盘：{next_session_str()}")
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
                flush_outbox()  # 双保险：不等 cron，立即推送
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

    def _adjust_for_drawdown(self, snapshot) -> None:
        """When all holdings are in the red, tighten risk controls.

        Automatically raises buy_score so the system doesn't keep buying
        into a losing streak. Restores to config value when any holding
        turns green.
        """
        if snapshot is None or not snapshot.holdings:
            return

        active = [h for h in snapshot.holdings if h.quantity > 0]
        if not active:
            return

        # Remember the original buy_score from config (first run only)
        if not hasattr(self, '_original_buy_score'):
            self._original_buy_score = self.config.monitor.decision_thresholds.buy_score

        # Check if ALL active holdings are losing money
        all_red = all(
            h.cost_price > 0 and h.current_price < h.cost_price
            for h in active
        )

        if all_red:
            total_loss_pct = sum(
                float((h.current_price - h.cost_price) / h.cost_price * 100)
                for h in active
            )
            avg_loss = total_loss_pct / len(active)
            base = self._original_buy_score
            if avg_loss < -3:
                self.config.monitor.decision_thresholds.buy_score = base + 6
                logger.info(
                    "回撤管理激活：全仓浮亏(均%.1f%%)，买入门槛 %s→%s",
                    avg_loss, base, base + 6,
                )
            else:
                self.config.monitor.decision_thresholds.buy_score = base + 3
        else:
            # Normal mode: restore original threshold
            self.config.monitor.decision_thresholds.buy_score = self._original_buy_score

    def _detect_and_log_trades(self, snapshot) -> None:
        """Detect portfolio changes vs last known snapshot and auto-log trades."""
        if snapshot is None:
            return
        try:
            # Build a compact fingerprint of current holdings
            holdings_data = [
                {"code": h.code, "name": h.name, "qty": h.quantity, "cost": float(h.cost_price)}
                for h in snapshot.holdings
            ]
            current_hash = hashlib.md5(
                json.dumps(holdings_data, sort_keys=True, default=str).encode()
            ).hexdigest()

            if not self._last_snapshot_hash:
                self._last_snapshot_hash = current_hash
                return
            if current_hash == self._last_snapshot_hash:
                return

            # Snapshot changed — try to load previous to diff
            prev_path = Path(self.config.portfolio.data_dir) / "snapshot_prev.json"
            prev_snapshot = None
            if prev_path.exists():
                prev_snapshot = load_portfolio_snapshot(prev_path)

            if prev_snapshot is None:
                self._last_snapshot_hash = current_hash
                # Save current as baseline for next diff
                with open(prev_path, "w") as f:
                    f.write(self.config.snapshot_path.read_text())
                return

            # Diff holdings
            prev_map = {h.code: h for h in prev_snapshot.holdings}
            curr_map = {h.code: h for h in snapshot.holdings}

            for code, curr_h in curr_map.items():
                prev_h = prev_map.get(code)
                if prev_h is None:
                    # New stock appeared — log buy
                    self._trade_journal.log_buy(
                        symbol=f"{'sh' if code.startswith('6') else 'sz'}{code}",
                        name=curr_h.name,
                        quantity=curr_h.quantity,
                        price=curr_h.cost_price,
                        reason=f"用户手动买入 {curr_h.quantity}股@{float(curr_h.cost_price):.2f}",
                        strategy="manual",
                        confidence="medium",
                        market_context="自动检测",
                    )
                    logger.info("Auto-logged BUY: %s %d股@%.2f", curr_h.name, curr_h.quantity, float(curr_h.cost_price))
                elif curr_h.quantity != prev_h.quantity:
                    delta = curr_h.quantity - prev_h.quantity
                    if delta > 0:
                        # Added more shares
                        self._trade_journal.log_buy(
                            symbol=f"{'sh' if code.startswith('6') else 'sz'}{code}",
                            name=curr_h.name,
                            quantity=delta,
                            price=curr_h.cost_price,
                            reason=f"用户手动加仓 {delta}股@{float(curr_h.cost_price):.2f}",
                            strategy="manual",
                            confidence="medium",
                            market_context="自动检测",
                        )
                        logger.info("Auto-logged ADD: %s +%d股", curr_h.name, delta)
                    else:
                        # Sold shares
                        self._trade_journal.log_sell(
                            symbol=f"{'sh' if code.startswith('6') else 'sz'}{code}",
                            name=curr_h.name,
                            quantity=abs(delta),
                            price=curr_h.cost_price,
                            reason=f"用户手动卖出 {abs(delta)}股@{float(curr_h.cost_price):.2f}",
                            buy_price=float(prev_h.cost_price) if prev_h.cost_price > 0 else None,
                            buy_date=prev_h.trade_date.isoformat() if prev_h.trade_date else None,
                            strategy="manual",
                            confidence="medium",
                            market_context="自动检测",
                        )
                        logger.info("Auto-logged SELL: %s -%d股", curr_h.name, abs(delta))

            for code, prev_h in prev_map.items():
                if code not in curr_map:
                    # Stock disappeared — log full sell
                    self._trade_journal.log_sell(
                        symbol=f"{'sh' if code.startswith('6') else 'sz'}{code}",
                        name=prev_h.name,
                        quantity=prev_h.quantity,
                        price=prev_h.cost_price,  # approximate — actual sell price unknown
                        reason=f"用户清仓 {prev_h.name}（{prev_h.quantity}股）",
                        buy_price=float(prev_h.cost_price) if prev_h.cost_price > 0 else None,
                        buy_date=prev_h.trade_date.isoformat() if prev_h.trade_date else None,
                        strategy="manual",
                        confidence="medium",
                        market_context="自动检测-清仓",
                    )
                    logger.info("Auto-logged CLEAR: %s %d股", prev_h.name, prev_h.quantity)

            # Save current snapshot as baseline for next diff
            with open(prev_path, "w") as f:
                f.write(self.config.snapshot_path.read_text())
            self._last_snapshot_hash = current_hash

        except Exception as exc:
            logger.warning("Trade auto-logger failed: %s", exc)

    def _load_portfolio_snapshot(self):
        if not self.config.snapshot_path.exists():
            return None
        return load_portfolio_snapshot(self.config.snapshot_path)

    def _build_provider(self):
        if self.config.monitor.provider == "eastmoney_minute":
            return EastmoneyMinuteHistoryProvider(self.config.monitor)
        return TencentQuoteProvider(self.config.monitor)


def _save_pre_market_state(data_dir: Path, today: date, lines: list[str], snapshot) -> None:
    """Save the pre-market briefing as JSON for Hermes to query."""
    import json as _json
    state_dir = data_dir / "briefing"
    state_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "date": today.isoformat(),
        "generated_at": datetime.now(MARKET_TZ).isoformat(),
        "summary": "\n".join(lines),
    }
    if snapshot:
        state["holdings"] = [
            {
                "name": h.name,
                "code": h.code,
                "quantity": h.quantity,
                "cost_price": float(h.cost_price),
                "current_price": float(h.current_price),
                "pnl_pct": float(((h.current_price - h.cost_price) / h.cost_price * 100) if h.cost_price > 0 else 0),
            }
            for h in snapshot.holdings if h.quantity > 0
        ]
    (state_dir / "latest.json").write_text(_json.dumps(state, ensure_ascii=False, indent=2))
