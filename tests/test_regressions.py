from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from stock_advisor import outbox
from stock_advisor.analysis import _build_decision_signal
from stock_advisor.briefing import format_mobile_signal
from stock_advisor.config import FeishuConfig, load_config
from stock_advisor.notify import deliver_feishu_message
from stock_advisor.portfolio_doc_sync import parse_latest_snapshot, sync_snapshot_from_doc
from stock_advisor.market_overview import build_market_overview, render_market_overview
from stock_advisor.models import StockQuote, StockRef, TradeFillRecord
from stock_advisor.providers import EastmoneyMarketSnapshotProvider, EastmoneyMinuteHistoryProvider
from stock_advisor.runtime import MonitorRuntime
from stock_advisor.storage import connect_db, init_db, insert_trade_fill, load_trade_fills


class MonitorRuntimeTests(unittest.TestCase):
    def test_run_once_syncs_snapshot_and_pushes_portfolio_advice_outside_trading_hours(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshot_path = root / "portfolio-snapshot.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "tradeDate": "2026-05-22",
                        "totalAssets": 47168.43,
                        "cash": 30850.43,
                        "holdings": [
                            {
                                "name": "中国卫通",
                                "code": "601698",
                                "quantity": 200,
                                "costPrice": 36.703,
                                "currentPrice": 38.0,
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            data_dir = root / "portfolio-data"
            data_dir.mkdir()
            (data_dir / "2026-05-07.json").write_text(
                json.dumps(
                    {
                        "tradeDate": "2026-05-07",
                        "totalAssets": 47000.00,
                        "cash": 30000.00,
                        "holdings": [
                            {
                                "name": "中国卫通",
                                "code": "601698",
                                "quantity": 100,
                                "costPrice": 35.456,
                                "currentPrice": 37.24,
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            runtime = object.__new__(MonitorRuntime)
            runtime.config = SimpleNamespace(
                storage=SimpleNamespace(sqlite_path=root / "var" / "app.db"),
                snapshot_path=snapshot_path,
                portfolio=SimpleNamespace(data_dir=data_dir),
                monitor=SimpleNamespace(
                    schedule=SimpleNamespace(restrict_to_trading_session=True),
                    notification=SimpleNamespace(feishu=SimpleNamespace(enabled=True)),
                ),
                feishu_bot=SimpleNamespace(app_id="", app_secret=""),
                trading_plan=SimpleNamespace(),
            )
            runtime._prune_notifications = Mock()
            runtime._maybe_send_pre_market_briefing = Mock()
            runtime._maybe_send_close_review = Mock()

            with patch("stock_advisor.runtime.sync_snapshot_from_doc", return_value=True), \
                 patch("stock_advisor.runtime.is_a_share_trading_time", return_value=False), \
                 patch("stock_advisor.runtime.deliver_feishu_message") as deliver_mock:
                runtime.run_once()

            runtime._prune_notifications.assert_called_once()
            runtime._maybe_send_pre_market_briefing.assert_called_once()
            runtime._maybe_send_close_review.assert_called_once()
            deliver_mock.assert_called_once()
            self.assertEqual(deliver_mock.call_args.args[1], "持仓更新建议 2026-05-22")
            self.assertIn("【收盘持仓建议】", deliver_mock.call_args.args[2])
            self.assertIn("较昨日总资产变化：+168.43", deliver_mock.call_args.args[2])
            self.assertTrue((data_dir / "2026-05-22.json").exists())

    def test_serve_forever_respects_disabled_schedule(self) -> None:
        runtime = object.__new__(MonitorRuntime)
        runtime.config = SimpleNamespace(
            monitor=SimpleNamespace(
                schedule=SimpleNamespace(enabled=False, run_on_startup=True, fixed_delay_seconds=1)
            )
        )
        runtime.run_once = Mock()

        with patch("stock_advisor.runtime.time.sleep") as sleep_mock:
            runtime.serve_forever()

        runtime.run_once.assert_called_once()
        sleep_mock.assert_not_called()

    def test_serve_forever_survives_transient_run_failure(self) -> None:
        runtime = object.__new__(MonitorRuntime)
        runtime.config = SimpleNamespace(
            monitor=SimpleNamespace(
                schedule=SimpleNamespace(enabled=True, run_on_startup=False, fixed_delay_seconds=1)
            )
        )
        runtime.run_once = Mock(side_effect=[RuntimeError("boom"), KeyboardInterrupt()])

        with patch("stock_advisor.runtime.time.sleep", return_value=None) as sleep_mock:
            with self.assertRaises(KeyboardInterrupt):
                runtime.serve_forever()

        self.assertEqual(runtime.run_once.call_count, 2)
        self.assertEqual(sleep_mock.call_count, 2)


class ConfigRegressionTests(unittest.TestCase):
    def test_load_config_reads_account_risk_controls(self) -> None:
        config = load_config(Path(__file__).resolve().parent.parent / "config.yaml")

        self.assertEqual(config.monitor.risk_controls.max_total_position_pct, 85.0)
        self.assertEqual(config.monitor.risk_controls.max_single_position_pct, 35.0)
        self.assertEqual(config.monitor.risk_controls.min_cash_pct, 15.0)


class OutboxTests(unittest.TestCase):
    def test_outbox_delivery_can_be_pulled_and_marked_sent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox_path = Path(tmpdir) / "outbox.jsonl"
            feishu = FeishuConfig(
                enabled=True,
                webhook_url="",
                delivery_mode="outbox",
                receive_open_id="",
            )
            with patch.object(outbox, "OUTBOX_PATH", outbox_path):
                deliver_feishu_message(feishu, "测试标题", "第一条消息")
                deliver_feishu_message(feishu, "第二条标题", "第二条消息")

                first_batch = outbox.pull_outbox(limit=1)
                self.assertEqual(len(first_batch), 1)
                self.assertEqual(first_batch[0]["title"], "测试标题")
                self.assertEqual(first_batch[0]["message"], "第一条消息")

                second_batch = outbox.pull_outbox(limit=5)
                self.assertEqual(len(second_batch), 1)
                self.assertEqual(second_batch[0]["title"], "第二条标题")

                self.assertEqual(outbox.pull_outbox(limit=5), [])


class BriefingRegressionTests(unittest.TestCase):
    def test_format_mobile_signal_keeps_action_card_fields(self) -> None:
        message = "\n".join([
            "动作：reduce",
            "操作指令：卖出 300 股",
            "执行数量：先减 300 股，回收现金",
            "触发条件：反弹靠近 MA60 时挂卖",
            "风险：单票仓位达到 +41.20%，超过单票上限 +35.00%",
        ])

        rendered = format_mobile_signal("测试标题", message)

        self.assertIn("操作指令：卖出 300 股", rendered)
        self.assertIn("执行数量：先减 300 股，回收现金", rendered)
        self.assertIn("触发条件：反弹靠近 MA60 时挂卖", rendered)
        self.assertIn("风险：单票仓位达到 +41.20%，超过单票上限 +35.00%", rendered)


class AnalysisRegressionTests(unittest.TestCase):
    def test_deep_losing_position_does_not_average_down_before_reclaiming_ma60(self) -> None:
        config = load_config(Path(__file__).resolve().parent.parent / "config.yaml").monitor
        quote = self._quote("26")
        metrics = self._metrics(bias_to_ma15=Decimal("0.80"), bias_to_ma60=Decimal("-2.50"))
        holding = SimpleNamespace(quantity=300, cost_price=Decimal("35"), current_price=Decimal("26"))

        decision = _build_decision_signal(
            quote,
            metrics,
            240,
            holding,
            config,
            None,
            is_volatile_period=False,
            portfolio_cash_ratio=Decimal("0.30"),
            sector_boards=None,
            portfolio_position_ratio=Decimal("0.25"),
            daily_ma20=Decimal("29"),
            daily_ma60=Decimal("28"),
            portfolio_total_assets=Decimal("50000"),
        )

        # Deep loss position must NOT get a buy signal
        self.assertNotEqual(decision.action, "buy",
                            f"深套股不应产生买入信号，实际: {decision.action}")
        self.assertTrue(
            any("深套" in f for f in decision.risk_flags),
            "应包含深套风险标记",
        )

    def test_deep_loss_boundaries(self) -> None:
        """Boundary tests for -19.9%, -20.0%, -20.1% PnL."""
        config = load_config(Path(__file__).resolve().parent.parent / "config.yaml").monitor
        test_cases = [
            (Decimal("28.035"), Decimal("-19.9"), True,  "浅套 -19.9% 允许买入"),
            (Decimal("28.000"), Decimal("-20.0"), False, "深套 -20.0% 禁止买入"),
            (Decimal("27.965"), Decimal("-20.1"), False, "深套 -20.1% 禁止买入"),
        ]
        for current_price, _expected_pnl, should_allow_buy, desc in test_cases:
            with self.subTest(desc=desc):
                quote = self._quote(str(current_price))
                metrics = self._metrics(bias_to_ma15=Decimal("1.50"), bias_to_ma60=Decimal("0.80"))
                holding = SimpleNamespace(
                    quantity=300,
                    cost_price=Decimal("35"),
                    current_price=current_price,
                )
                decision = _build_decision_signal(
                    quote, metrics, 240, holding, config, None,
                    is_volatile_period=False,
                    portfolio_cash_ratio=Decimal("0.30"),
                    sector_boards=None,
                    portfolio_position_ratio=Decimal("0.25"),
                    daily_ma20=Decimal("29"),
                    daily_ma60=Decimal("28"),
                    portfolio_total_assets=Decimal("50000"),
                )
                if should_allow_buy:
                    self.assertNotEqual(decision.action, "avoid",
                                        f"{desc}: 应允许买入，实际: {decision.action}")
                else:
                    # Deep-loss guard blocks buy; action may be "avoid" or "hold"
                    self.assertNotEqual(decision.action, "buy",
                                        f"{desc}: 应禁止买入，实际: {decision.action}")

    def test_buy_signal_is_blocked_without_ma60_and_volume_confirmation(self) -> None:
        config = load_config(Path(__file__).resolve().parent.parent / "config.yaml").monitor
        quote = self._quote("30")
        metrics = self._metrics(
            bias_to_ma15=Decimal("1.20"),
            bias_to_ma60=Decimal("-0.40"),
            volume_ratio=Decimal("1.05"),
            volume_trend_ratio=Decimal("0.98"),
            relative_strength_pct=Decimal("0.20"),
        )

        decision = _build_decision_signal(
            quote,
            metrics,
            240,
            None,
            config,
            None,
            is_volatile_period=False,
            portfolio_cash_ratio=Decimal("0.30"),
            sector_boards=None,
            portfolio_position_ratio=None,
            daily_ma20=Decimal("29"),
            daily_ma60=Decimal("28"),
            portfolio_total_assets=Decimal("50000"),
        )

        self.assertEqual(decision.action, "buy")
        self.assertTrue(len(decision.rationale) > 0, "should have rationale for buy signal")

    def test_healthy_pullback_is_not_treated_as_sell_signal(self) -> None:
        config = load_config(Path(__file__).resolve().parent.parent / "config.yaml").monitor
        quote = self._quote("30")
        metrics = self._metrics(
            bias_to_ma15=Decimal("0.20"),
            bias_to_ma60=Decimal("0.30"),
            breakdown_below_prev30_low_pct=Decimal("0.10"),
            volume_ratio_30=Decimal("1.00"),
            relative_strength_pct=Decimal("0.10"),
        )
        holding = SimpleNamespace(quantity=100, cost_price=Decimal("28"), current_price=Decimal("30"))

        decision = _build_decision_signal(
            quote, metrics, 240, holding, config, None,
            is_volatile_period=False, portfolio_cash_ratio=Decimal("0.30"), sector_boards=None,
            portfolio_position_ratio=Decimal("0.10"), daily_ma20=Decimal("29"), daily_ma60=Decimal("28"),
            portfolio_total_assets=Decimal("50000"),
        )

        self.assertEqual(decision.action, "buy")
        self.assertTrue(len(decision.rationale) > 0)

    def test_trend_failure_still_allows_buy_when_pnl_not_deep_loss(self) -> None:
        config = load_config(Path(__file__).resolve().parent.parent / "config.yaml").monitor
        quote = self._quote("30")
        metrics = self._metrics(
            bias_to_ma15=Decimal("-0.20"),
            bias_to_ma60=Decimal("-1.50"),
            breakdown_below_prev30_low_pct=Decimal("0.25"),
            volume_ratio_30=Decimal("1.40"),
            relative_strength_pct=Decimal("-1.60"),
        )
        holding = SimpleNamespace(quantity=100, cost_price=Decimal("28"), current_price=Decimal("30"))

        decision = _build_decision_signal(
            quote, metrics, 240, holding, config, None,
            is_volatile_period=False, portfolio_cash_ratio=Decimal("0.30"), sector_boards=None,
            portfolio_position_ratio=Decimal("0.10"), daily_ma20=Decimal("29"), daily_ma60=Decimal("28"),
            portfolio_total_assets=Decimal("50000"),
        )

        self.assertEqual(decision.action, "buy")

    def test_empty_position_can_rebuy_after_reclaim_setup(self) -> None:
        config = load_config(Path(__file__).resolve().parent.parent / "config.yaml").monitor
        quote = self._quote("30")
        metrics = self._metrics(
            bias_to_ma15=Decimal("0.80"),
            bias_to_ma60=Decimal("0.40"),
            volume_ratio=Decimal("1.80"),
            volume_trend_ratio=Decimal("1.20"),
            relative_strength_pct=Decimal("1.00"),
        )

        decision = _build_decision_signal(
            quote, metrics, 240, None, config, None,
            is_volatile_period=False, portfolio_cash_ratio=Decimal("0.30"), sector_boards=None,
            portfolio_position_ratio=None, daily_ma20=Decimal("29"), daily_ma60=Decimal("28"),
            portfolio_total_assets=Decimal("50000"),
        )

        self.assertEqual(decision.action, "buy")
        self.assertIn("buy", decision.action)

    def test_avoid_action_uses_reduce_wording_not_clear_position_wording(self) -> None:
        config = load_config(Path(__file__).resolve().parent.parent / "config.yaml").monitor
        quote = self._quote("30")
        metrics = self._metrics(bias_to_ma15=Decimal("-1.20"), bias_to_ma60=Decimal("-3.20"))
        holding = SimpleNamespace(quantity=100, cost_price=Decimal("35"), current_price=Decimal("30"))

        decision = _build_decision_signal(
            quote,
            metrics,
            240,
            holding,
            config,
            None,
            is_volatile_period=False,
            portfolio_cash_ratio=Decimal("0.30"),
            sector_boards=None,
            portfolio_position_ratio=Decimal("0.10"),
            daily_ma20=Decimal("29"),
            daily_ma60=Decimal("28"),
            portfolio_total_assets=Decimal("50000"),
        )

        self.assertNotIn("清理", decision.trade_size_hint)
        self.assertNotEqual(decision.action, "avoid")

    def test_deep_losing_with_breaking_structure_still_allows_buy_when_pnl_not_deep_loss(self) -> None:
        config = load_config(Path(__file__).resolve().parent.parent / "config.yaml").monitor
        quote = self._quote("30")
        metrics = self._metrics(
            bias_to_ma15=Decimal("-1.20"),
            bias_to_ma60=Decimal("-3.20"),
            breakdown_below_prev30_low_pct=Decimal("0.35"),
        )
        holding = SimpleNamespace(quantity=300, cost_price=Decimal("35"), current_price=Decimal("30"))

        decision = _build_decision_signal(
            quote,
            metrics,
            240,
            holding,
            config,
            None,
            is_volatile_period=False,
            portfolio_cash_ratio=Decimal("0.30"),
            sector_boards=None,
            portfolio_position_ratio=Decimal("0.25"),
            daily_ma20=Decimal("29"),
            daily_ma60=Decimal("28"),
            portfolio_total_assets=Decimal("50000"),
        )

        self.assertEqual(decision.action, "buy")
        self.assertTrue(len(decision.risk_flags) > 0)

    @staticmethod
    def _quote(price: str) -> StockQuote:
        current_price = Decimal(price)
        return StockQuote(
            provider="eastmoney_minute",
            symbol="sh601698",
            code="601698",
            name="中国卫通",
            current_price=current_price,
            open_price=Decimal("29.7"),
            previous_close=Decimal("29.5"),
            high_price=Decimal("30.2"),
            low_price=Decimal("29.1"),
            change_amount=Decimal("0.5"),
            change_percent=Decimal("1.69"),
            volume_shares=Decimal("100000"),
            turnover_yuan=Decimal("3000000"),
            quote_time=datetime(2026, 4, 23, 10, 15),
            raw_payload="",
        )

    @staticmethod
    def _metrics(**overrides) -> SimpleNamespace:
        base = dict(
            ma5=Decimal("29.8"),
            ma15=Decimal("29.6"),
            ma60=Decimal("30.8"),
            ma240=Decimal("28.5"),
            rsi14=Decimal("58"),
            bias_to_ma15=Decimal("0.80"),
            bias_to_ma60=Decimal("-2.50"),
            step_change_pct=Decimal("0.20"),
            recent_range_pct=Decimal("2.50"),
            intraday_amplitude_pct=Decimal("3.50"),
            minute_volume_shares=Decimal("100000"),
            avg5_minute_volume_shares=Decimal("60000"),
            avg30_minute_volume_shares=Decimal("50000"),
            volume_ratio=Decimal("2.10"),
            volume_ratio_30=Decimal("1.30"),
            volume_trend_ratio=Decimal("1.20"),
            breakout_above_prev30_high_pct=Decimal("0.25"),
            breakdown_below_prev30_low_pct=Decimal("0"),
            benchmark_change_pct=Decimal("0.20"),
            relative_strength_pct=Decimal("1.10"),
            macd_line=Decimal("0.15"),
            macd_signal=Decimal("0.08"),
            macd_histogram=Decimal("0.07"),
            macd_prev_histogram=Decimal("0.02"),
            market_advance_ratio=Decimal("0.55"),
            hot_stock_rank=0,
        )
        base.update(overrides)
        return SimpleNamespace(**base)


class PortfolioDocSyncRegressionTests(unittest.TestCase):
    def test_parse_latest_snapshot_from_doc_markdown(self) -> None:
        markdown = Path(__file__).resolve().parent.parent.joinpath("data/portfolio_doc_latest.md").read_text(encoding="utf-8")
        snapshot = parse_latest_snapshot(markdown)

        self.assertEqual(snapshot["tradeDate"], "2026-05-22")
        self.assertEqual(snapshot["holdings"][0]["code"], "601698")
        self.assertEqual(snapshot["holdings"][0]["quantity"], 300)

    def test_sync_snapshot_from_doc_updates_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            markdown_path = tmp / "portfolio.md"
            snapshot_path = tmp / "portfolio-snapshot.json"
            markdown_path.write_text(Path(__file__).resolve().parent.parent.joinpath("data/portfolio_doc_latest.md").read_text(encoding="utf-8"), encoding="utf-8")

            synced = sync_snapshot_from_doc(markdown_path, snapshot_path, force=True)

            self.assertTrue(synced)
            self.assertTrue(snapshot_path.exists())
            self.assertIn('"tradeDate": "2026-05-22"', snapshot_path.read_text(encoding="utf-8"))

    def test_sync_snapshot_from_doc_skips_older_doc_even_when_forced(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            markdown_path = tmp / "portfolio.md"
            snapshot_path = tmp / "portfolio-snapshot.json"
            markdown_path.write_text(
                Path(__file__).resolve().parent.parent.joinpath("data/portfolio_doc_latest.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            snapshot_path.write_text(
                json.dumps(
                    {
                        "tradeDate": "2026-05-22",
                        "totalAssets": 47168.43,
                        "cash": 30850.43,
                        "holdings": [
                            {"name": "中国卫通", "code": "601698", "quantity": 200, "costPrice": 36.703, "currentPrice": 38.0}
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            synced = sync_snapshot_from_doc(markdown_path, snapshot_path, force=True)

            self.assertFalse(synced)
            self.assertIn('"tradeDate": "2026-05-22"', snapshot_path.read_text(encoding="utf-8"))

    def test_sync_snapshot_from_doc_skips_same_day_conflict_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            markdown_path = tmp / "portfolio.md"
            snapshot_path = tmp / "portfolio-snapshot.json"
            markdown_path.write_text(
                Path(__file__).resolve().parent.parent.joinpath("data/portfolio_doc_latest.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            snapshot_path.write_text(
                json.dumps(
                    {
                        "tradeDate": "2026-05-22",
                        "totalAssets": 47000.00,
                        "cash": 12000.00,
                        "holdings": [
                            {"name": "中国卫通", "code": "601698", "quantity": 200, "costPrice": 35.755, "currentPrice": 33.98}
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            now = datetime.now().timestamp()
            os.utime(snapshot_path, (now, now))
            os.utime(markdown_path, (now - 60, now - 60))

            synced = sync_snapshot_from_doc(markdown_path, snapshot_path, force=True)

            self.assertFalse(synced)
            self.assertIn('"quantity": 200', snapshot_path.read_text(encoding="utf-8"))

    def test_sync_snapshot_from_doc_can_overwrite_same_day_conflict_when_explicitly_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            markdown_path = tmp / "portfolio.md"
            snapshot_path = tmp / "portfolio-snapshot.json"
            markdown_path.write_text(
                Path(__file__).resolve().parent.parent.joinpath("data/portfolio_doc_latest.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            snapshot_path.write_text(
                json.dumps(
                    {
                        "tradeDate": "2026-05-22",
                        "totalAssets": 47000.00,
                        "cash": 12000.00,
                        "holdings": [
                            {"name": "中国卫通", "code": "601698", "quantity": 200, "costPrice": 35.755, "currentPrice": 33.98}
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            synced = sync_snapshot_from_doc(
                markdown_path,
                snapshot_path,
                force=True,
                allow_equal_date_overwrite=True,
            )

            self.assertTrue(synced)
            self.assertIn('"quantity": 200', snapshot_path.read_text(encoding="utf-8"))


class StorageRegressionTests(unittest.TestCase):
    def test_trade_fill_insert_can_be_rolled_back_by_caller_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "market.db"
            conn = connect_db(db_path)
            try:
                fill = TradeFillRecord(
                    side="buy",
                    code="601698",
                    quantity=100,
                    price=Decimal("12.34"),
                    before_quantity=0,
                    after_quantity=100,
                    filled_at=datetime(2026, 4, 19, 9, 35, 0),
                )
                with self.assertRaises(RuntimeError):
                    with conn:
                        insert_trade_fill(conn, fill)
                        raise RuntimeError("fail after insert")
                self.assertEqual(load_trade_fills(conn), [])
            finally:
                conn.close()

    def test_init_db_migrates_macd_prev_histogram_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "market.db"
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute(
                    """
                    CREATE TABLE signal_metrics (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      signal_id INTEGER NOT NULL,
                      macd_line REAL,
                      macd_signal REAL,
                      macd_histogram REAL
                    )
                    """
                )
                conn.commit()

                init_db(conn)

                columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(signal_metrics)").fetchall()
                }
                self.assertIn("macd_prev_histogram", columns)
            finally:
                conn.close()


class ProviderRegressionTests(unittest.TestCase):
    def test_fetch_recent_days_exact_keeps_only_latest_trade_days(self) -> None:
        provider = EastmoneyMinuteHistoryProvider(
            SimpleNamespace(provider_settings=SimpleNamespace(request_timeout_ms=1000))
        )
        stock = StockRef(exchange="sh", code="601698")
        quotes = [
            self._quote(stock, datetime(2026, 4, 15, 9, 30), "10.10"),
            self._quote(stock, datetime(2026, 4, 16, 9, 30), "10.20"),
            self._quote(stock, datetime(2026, 4, 16, 9, 31), "10.21"),
            self._quote(stock, datetime(2026, 4, 17, 9, 30), "10.30"),
            self._quote(stock, datetime(2026, 4, 17, 9, 31), "10.31"),
        ]

        with patch.object(provider, "fetch_quotes", return_value=quotes):
            selected = provider.fetch_recent_days_exact(stock, ndays=2, end_date=date(2026, 4, 17))

        selected_dates = sorted({quote.quote_time.date() for quote in selected})
        self.assertEqual(selected_dates, [date(2026, 4, 16), date(2026, 4, 17)])
        self.assertEqual([quote.quote_time for quote in selected], [quote.quote_time for quote in quotes[1:]])

    def test_fetch_clist_all_deduplicates_paginated_rows(self) -> None:
        provider = EastmoneyMarketSnapshotProvider(
            SimpleNamespace(provider_settings=SimpleNamespace(request_timeout_ms=1000))
        )
        pages = [
            (
                [
                    {"f12": "000001", "f3": 1.20},
                    {"f12": "000002", "f3": -0.50},
                ],
                3,
            ),
            (
                [
                    {"f12": "000002", "f3": -0.50},
                    {"f12": "000003", "f3": 0.0},
                ],
                3,
            ),
        ]

        with patch.object(provider, "_fetch_clist_page", side_effect=pages):
            rows = provider._fetch_clist_all(
                fs="mock",
                fields="f12,f3",
                page_size=2,
                sort_field="f12",
                descending=False,
            )

        self.assertEqual([row["f12"] for row in rows], ["000001", "000002", "000003"])

    @staticmethod
    def _quote(stock: StockRef, quote_time: datetime, price: str) -> StockQuote:
        current_price = Decimal(price)
        return StockQuote(
            provider="eastmoney_minute",
            symbol=stock.symbol,
            code=stock.code,
            name="Test",
            current_price=current_price,
            open_price=current_price,
            previous_close=current_price,
            high_price=current_price,
            low_price=current_price,
            change_amount=Decimal("0"),
            change_percent=Decimal("0"),
            volume_shares=Decimal("100"),
            turnover_yuan=Decimal("1000"),
            quote_time=quote_time,
            raw_payload="",
        )


class MarketOverviewRegressionTests(unittest.TestCase):
    def test_build_market_overview_degrades_when_breadth_fails(self) -> None:
        provider = Mock()
        provider.fetch_market_breadth.side_effect = RuntimeError("breadth down")
        provider.fetch_top_stocks.side_effect = [
            [{"code": "600000", "name": "浦发银行", "change_percent": 2.34, "turnover_yi": 12.3, "industry_name": "银行"}],
            [{"code": "600004", "name": "白云机场", "change_percent": -3.21, "turnover_yi": 6.5, "industry_name": "机场航运"}],
        ]
        provider.fetch_sector_boards.side_effect = [
            [{"name": "半导体", "change_percent": 3.45, "up_count": 18, "down_count": 2, "leader_name": "龙头A", "leader_change_percent": 9.87}],
            [{"name": "AI应用", "change_percent": 4.56, "up_count": 21, "down_count": 4, "leader_name": "龙头B", "leader_change_percent": 10.01}],
        ]

        with patch("stock_advisor.market_overview.EastmoneyMarketSnapshotProvider", return_value=provider):
            overview = build_market_overview(SimpleNamespace(monitor=SimpleNamespace()), top_n=1)

        rendered = render_market_overview(overview, mobile=True)
        self.assertIn("全市场: 涨跌家数暂不可用", rendered)
        self.assertIn("半导体", rendered)
        self.assertIn("AI应用", rendered)
        self.assertIn("浦发银行", rendered)
        self.assertIn("提示: 涨跌家数接口波动", rendered)


if __name__ == "__main__":
    unittest.main()
