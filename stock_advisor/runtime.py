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
from .market_hours import MARKET_TZ, is_a_share_trading_time, is_auction_period, is_high_volatility_period, is_opening_grace_period, next_session_str, seconds_until_next_session
from .models import StockQuote
from .logging_utils import get_logger
from .news import fetch_announcements_for_code, filter_new_announcements, format_announcement_line
from .notify import deliver_feishu_message
from .codex_bridge import flush_codex_bridge
from .portfolio import compute_cash_ratio, compute_position_ratio, find_holding, generate_portfolio_report, load_snapshot as load_portfolio_snapshot
from .portfolio_doc_sync import sync_snapshot_from_doc
from .providers import EastmoneyMarketSnapshotProvider, EastmoneyMinuteHistoryProvider, TencentQuoteProvider
from .review import already_sent_close_review, build_close_review, find_latest_plan_record, mark_close_review_sent, should_send_close_review_now
from .stop_loss import compute_effective_stop
from .storage import cache_quotes, connect_db, load_recent_quotes, persist_observation
from .trading_plan import build_risk_context, detect_trigger_hit, load_snapshot as load_trade_snapshot, load_triggers, render_trade_instruction


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
            )
            persist_observation(self.db, quote, result)
            logger.info("=" * 80)  # type: ignore[arg-type]  # logging format interprets % as placeholder escape
            logger.info(result.title)  # type: ignore[arg-type]
            logger.info(result.message)  # type: ignore[arg-type]

            trigger_message = self._build_trigger_message(quote)
            if trigger_message:
                # Secondary confirmation: if scoring engine says "avoid",
                # suppress trigger notification (e.g. crash day, deep loss, etc.)
                if result.decision.action == "avoid":
                    logger.warning(
                        "Trigger hit suppressed by scoring engine: %s %s (score=%s, action=%s)",
                        quote.code, quote.name, result.decision.score, result.decision.action,
                    )
                else:
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
            except Exception:
                pass  # Non-critical — don't crash the daemon for missing config

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
        # Mute low-confidence neutral zone signals: score 45-55 with low confidence
        # are noise that distracts office workers without actionable value.
        confidence = result.decision.confidence
        score = result.decision.score
        if confidence == "low" and Decimal("45") <= score <= Decimal("55") and action in ("hold", "avoid"):
            return False
        if action == "buy":
            return True
        has_position = holding is not None and holding.quantity > 0
        if action in ("reduce", "avoid") and has_position:
            # Opening grace period (9:30-9:45): mute all reduce/avoid signals.
            # Minute-level MA signals are extremely unreliable right after open;
            # the first 15 minutes are pure noise.  Let the market settle first.
            if is_opening_grace_period():
                return False
            return True
        return False

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
                flush_codex_bridge()  # 双保险：不等 cron，立即推送
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
                        except Exception:
                            pass
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
        except Exception:
            pass

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
                except Exception:
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
                        verdict = "🟡 浮亏中，等反弹减仓"
                    quick_verdicts.append(f"- {holding.name}：{verdict}")
                if quick_verdicts:
                    lines.append("\n【今日速判】")
                    lines.extend(quick_verdicts)
            except Exception:
                pass

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
        except Exception:
            pass

        # Save briefing data for status command
        try:
            _save_pre_market_state(Path("data"), today, lines, snapshot)
        except Exception:
            pass

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
                flush_codex_bridge()  # 双保险：不等 cron，立即推送
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
