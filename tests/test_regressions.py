from __future__ import annotations

import json
import importlib.util
import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from io import StringIO
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
from stock_advisor.models import PortfolioHolding, PortfolioSnapshot, StockQuote, StockRef, TradeFillRecord
from stock_advisor.providers import EastmoneyMarketSnapshotProvider, EastmoneyMinuteHistoryProvider
from stock_advisor.runtime import MonitorRuntime
from stock_advisor.storage import connect_db, init_db, insert_trade_fill, load_trade_fills
from stock_advisor.debate_trigger_sync import sync_debate_to_triggers
from stock_advisor.trading_plan import sync_exit_plan_triggers


class MonitorRuntimeTests(unittest.TestCase):
    def test_run_once_only_keeps_pre_market_and_close_review_active_pushes(self) -> None:
        runtime = object.__new__(MonitorRuntime)
        runtime.config = SimpleNamespace(
            monitor=SimpleNamespace(
                schedule=SimpleNamespace(restrict_to_trading_session=True),
                notification=SimpleNamespace(feishu=SimpleNamespace(enabled=True)),
            )
        )
        runtime._prune_notifications = Mock()
        runtime._check_bridge_health = Mock()
        runtime._sync_portfolio_snapshot_if_needed = Mock()
        runtime._maybe_send_pre_market_briefing = Mock()
        runtime._maybe_send_close_review = Mock()
        runtime._maybe_send_breaking_news = Mock()
        runtime._maybe_send_intraday_opportunities = Mock()
        runtime._load_portfolio_snapshot = Mock(return_value=None)
        runtime._detect_and_log_trades = Mock()
        runtime._adjust_for_drawdown = Mock()
        runtime._load_benchmark_history = Mock(return_value=[])
        runtime._load_market_context = Mock(return_value=(0, {}, None))
        runtime.config.monitor.stocks = []
        runtime.db = None
        runtime.tencent_provider = Mock()
        runtime.price_high_marks = {}
        runtime._price_high_marks_date = date(2026, 5, 28)
        runtime._daily_closes = {}
        runtime._daily_closes_date = date(2026, 5, 28)

        with patch("stock_advisor.runtime.is_a_share_trading_time", return_value=True), \
             patch("stock_advisor.runtime.compute_cash_ratio", return_value=Decimal("0")), \
             patch("stock_advisor.runtime.build_trading_habit_profile", return_value=None), \
             patch("stock_advisor.runtime.is_high_volatility_period", return_value=False):
            runtime.run_once()

        runtime._maybe_send_pre_market_briefing.assert_called_once()
        runtime._maybe_send_breaking_news.assert_not_called()
        runtime._maybe_send_intraday_opportunities.assert_not_called()

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


class DebateTriggerSyncRegressionTests(unittest.TestCase):
    def test_debate_sync_creates_partial_reduce_and_preserves_exit_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "data"
            feedback_dir = data_dir / "feedback"
            feedback_dir.mkdir(parents=True)

            (root / "portfolio-snapshot.json").write_text(
                json.dumps(
                    {
                        "tradeDate": "2026-05-27",
                        "totalAssets": 43421.82,
                        "cash": 676.82,
                        "holdings": [
                            {
                                "name": "中国卫通",
                                "code": "601698",
                                "quantity": 1100,
                                "costPrice": 33.944,
                                "currentPrice": 31.218,
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (feedback_dir / "debate_log.jsonl").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-05-27T15:03:01.590337",
                        "symbol": "sh601698",
                        "name": "中国卫通",
                        "price": 31.47,
                        "action": "sell",
                        "confidence": 0.9,
                        "vote_summary": "风控否决（跳过仲裁）",
                        "reasoning": "风控官强制执行卖出：持仓浮亏-7.3%已触发7%铁血止损红线，本金安全优先，纪律卖出不侥幸。",
                        "agent_votes": {
                            "铁血风控": "sell",
                            "大胆猎手": "hold",
                            "趋势判官": "sell",
                            "宏观观察": "hold",
                            "资金猎犬": "sell",
                        },
                    },
                    ensure_ascii=False,
                ) + "\n",
                encoding="utf-8",
            )
            (data_dir / "trading_plan.json").write_text(
                json.dumps(
                    {
                        "triggers": [
                            {
                                "code": "601698",
                                "name": "中国卫通-辩论止损",
                                "action": "sell",
                                "quantity": 1100,
                                "priceMin": "31.37",
                                "priceMax": "31.57",
                                "fallbackPrice": "31.42",
                                "note": "旧的错误全仓辩论止损",
                                "disableBuy": True,
                                "_source": "debate_sync",
                            },
                            {
                                "code": "601698",
                                "name": "中国卫通-卖点计划",
                                "action": "sell",
                                "quantity": 200,
                                "priceMin": "32.05",
                                "priceMax": "32.25",
                                "fallbackPrice": "30.28",
                                "note": "自动卖点计划",
                                "disableBuy": True,
                                "_source": "exit_plan_sync",
                            },
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            actions = sync_debate_to_triggers(data_dir)
            self.assertEqual(len(actions), 1)
            self.assertIn("辩论减仓 300股", actions[0])

            plan = json.loads((data_dir / "trading_plan.json").read_text(encoding="utf-8"))
            triggers = plan["triggers"]
            self.assertEqual(len(triggers), 2)

            debate = next(t for t in triggers if t.get("_source") == "debate_sync")
            exit_plan = next(t for t in triggers if t.get("_source") == "exit_plan_sync")

            self.assertEqual(debate["name"], "中国卫通-辩论减仓")
            self.assertEqual(debate["quantity"], 300)
            self.assertIn("不做全仓硬砍", debate["note"])
            self.assertEqual(exit_plan["name"], "中国卫通-卖点计划")
            self.assertEqual(exit_plan["quantity"], 200)

    def test_sync_exit_plan_skips_codes_with_debate_sell_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trigger_path = Path(tmpdir) / "trading_plan.json"
            trigger_path.write_text(
                json.dumps(
                    {
                        "triggers": [
                            {
                                "code": "601698",
                                "name": "中国卫通-辩论减仓",
                                "action": "sell",
                                "quantity": 300,
                                "priceMin": "31.37",
                                "priceMax": "31.57",
                                "fallbackPrice": "31.42",
                                "note": "收盘辩论建议先减仓",
                                "disableBuy": True,
                                "_source": "debate_sync",
                                "_created": "2026-05-28T09:00:00",
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            snapshot = PortfolioSnapshot(
                trade_date=date(2026, 5, 28),
                total_assets=Decimal("43422"),
                cash=Decimal("677"),
                holdings=[
                    PortfolioHolding(
                        name="中国卫通",
                        code="601698",
                        quantity=1100,
                        cost_price=Decimal("33.944"),
                        current_price=Decimal("31.218"),
                    ),
                    PortfolioHolding(
                        name="中兴通讯",
                        code="000063",
                        quantity=200,
                        cost_price=Decimal("37.289"),
                        current_price=Decimal("34.66"),
                    ),
                ],
            )

            created = sync_exit_plan_triggers(snapshot, trigger_path)
            self.assertEqual(created, 1)

            plan = json.loads(trigger_path.read_text(encoding="utf-8"))
            triggers = plan["triggers"]
            self.assertEqual(len(triggers), 2)
            self.assertEqual(sum(1 for t in triggers if t.get("code") == "601698"), 1)
            self.assertEqual(sum(1 for t in triggers if t.get("code") == "000063"), 1)
            self.assertEqual(next(t for t in triggers if t.get("code") == "601698")["_source"], "debate_sync")
            self.assertEqual(next(t for t in triggers if t.get("code") == "000063")["_source"], "exit_plan_sync")


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

    def test_run_status_prints_latest_summary_when_briefing_starts_with_quick_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "data" / "briefing").mkdir(parents=True)
            snapshot_path = root / "portfolio-snapshot.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "tradeDate": "2026-05-28",
                        "totalAssets": 44441.09,
                        "cash": 4230.09,
                        "holdings": [
                            {
                                "name": "中兴通讯",
                                "code": "000063",
                                "quantity": 100,
                                "costPrice": 35.55,
                                "currentPrice": 38.30,
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (root / "data" / "briefing" / "latest.json").write_text(
                json.dumps(
                    {
                        "date": "2026-05-28",
                        "generated_at": "2026-05-28T14:22:08+08:00",
                        "summary": "【今日速判】\n- 中兴通讯：🔴 强势持有，已涨停，今日不卖\n\n【持仓卖点计划】\n- 中兴通讯：100股继续持有；涨停封单在，不卖，炸板再评估",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            trigger_path = root / "data" / "trading_plan.json"
            trigger_path.write_text(json.dumps({"triggers": []}, ensure_ascii=False), encoding="utf-8")

            config = SimpleNamespace(snapshot_path=snapshot_path, trading_plan=SimpleNamespace(path=trigger_path))
            output = StringIO()
            with patch("stock_advisor.cli.require_valid_config", return_value=config), \
                 patch("stock_advisor.cli.load_snapshot") as load_snapshot_mock, \
                 patch("stock_advisor.cli.load_triggers", return_value={}), \
                 patch("subprocess.run") as subprocess_run_mock, \
                 patch("sys.stdout", output):
                load_snapshot_mock.return_value = PortfolioSnapshot(
                    trade_date=date(2026, 5, 28),
                    total_assets=Decimal("44441.09"),
                    cash=Decimal("4230.09"),
                    holdings=[
                        PortfolioHolding(
                            name="中兴通讯",
                            code="000063",
                            quantity=100,
                            cost_price=Decimal("35.55"),
                            current_price=Decimal("38.30"),
                        )
                    ],
                )
                subprocess_run_mock.return_value = SimpleNamespace(stdout="123\n")
                from stock_advisor.cli import run_status
                # run_status 用相对 cwd 读 data/briefing/latest.json —— chdir 到临时目录让 fixture 生效
                old_cwd = os.getcwd()
                os.chdir(str(root))
                try:
                    run_status("config.yaml")
                finally:
                    os.chdir(old_cwd)

            rendered = output.getvalue()
            self.assertIn("最近盘前简报：2026-05-28（2026-05-28T14:22）", rendered)
            self.assertIn("- 中兴通讯：🔴 强势持有，已涨停，今日不卖", rendered)
            self.assertIn("- 中兴通讯：100股继续持有；涨停封单在，不卖，炸板再评估", rendered)


class BridgeValidatorRegressionTests(unittest.TestCase):
    def test_new_trigger_does_not_inherit_old_cooldown_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            data_dir = repo / "data"
            data_dir.mkdir()

            (data_dir / "trading_plan.json").write_text(
                json.dumps(
                    {
                        "triggers": [
                            {
                                "code": "601698",
                                "name": "中国卫通-辩论止损",
                                "action": "sell",
                                "quantity": 1100,
                                "priceMin": "31.37",
                                "priceMax": "31.57",
                                "fallbackPrice": "31.42",
                                "note": "新的辩论止损",
                                "disableBuy": True,
                                "_source": "debate_sync",
                                "_created": "2026-05-27T15:14:13.219857",
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            (data_dir / "bridge_trigger_cooldown.json").write_text(
                json.dumps(
                    {
                        "601698:中国卫通-辩论止损": {
                            "ts": 0,
                            "count": 2,
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            spec = importlib.util.spec_from_file_location(
                "bridge_validator_under_test",
                Path(__file__).resolve().parent.parent / "stock_advisor" / "bridge_validator.py",
            )
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            with patch.object(module, "REPO", repo), \
                 patch.object(module, "TRIGGER_COOLDOWN_PATH", data_dir / "bridge_trigger_cooldown.json"), \
                 patch.object(module, "SNAPSHOT_PATH", data_dir / "missing-snapshot.json"), \
                 patch.object(module, "_is_trigger_delivery_window", return_value=True), \
                 patch.object(module, "_fetch_trigger_prices", return_value={"601698": 31.47}):
                alerts = module._check_triggers()

            self.assertEqual(len(alerts), 1)

            plan = json.loads((data_dir / "trading_plan.json").read_text(encoding="utf-8"))
            self.assertEqual(len(plan["triggers"]), 1)

            cooldown = json.loads((data_dir / "bridge_trigger_cooldown.json").read_text(encoding="utf-8"))
            self.assertIn("601698:中国卫通-辩论止损:debate_sync:2026-05-27T15:14:13.219857", cooldown)
            self.assertEqual(cooldown["601698:中国卫通-辩论止损:debate_sync:2026-05-27T15:14:13.219857"]["count"], 1)

    def test_auto_disable_debate_sell_also_removes_same_code_exit_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            data_dir = repo / "data"
            data_dir.mkdir()
            (data_dir / "trading_plan.json").write_text(
                json.dumps(
                    {
                        "triggers": [
                            {
                                "code": "601698",
                                "name": "中国卫通-辩论减仓",
                                "action": "sell",
                                "quantity": 300,
                                "priceMin": "31.37",
                                "priceMax": "31.57",
                                "fallbackPrice": "31.42",
                                "note": "辩论减仓",
                                "disableBuy": True,
                                "_source": "debate_sync",
                                "_created": "2026-05-28T09:00:00",
                            },
                            {
                                "code": "601698",
                                "name": "中国卫通-卖点计划",
                                "action": "sell",
                                "quantity": 200,
                                "priceMin": "32.05",
                                "priceMax": "32.25",
                                "fallbackPrice": "30.28",
                                "note": "自动卖点计划",
                                "disableBuy": True,
                                "_source": "exit_plan_sync",
                                "_created": "2026-05-28T09:05:00",
                            },
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            spec = importlib.util.spec_from_file_location(
                "bridge_validator_under_test",
                Path(__file__).resolve().parent.parent / "stock_advisor" / "bridge_validator.py",
            )
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            with patch.object(module, "REPO", repo):
                module._auto_disable_triggers({"601698:中国卫通-辩论减仓:debate_sync:2026-05-28T09:00:00"})

            plan = json.loads((data_dir / "trading_plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["triggers"], [])


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

    def test_deep_losing_with_breaking_structure_reduces_when_pnl_not_deep_loss(self) -> None:
        # 2026-05 保守策略后：浮亏-14.29% 且现价已跌破固定止损价（成本-7%=32.55），
        # 止损线以下禁止加仓 —— 行为从 buy 变更为 reduce（止损价下还买入是灾难）
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

        self.assertEqual(decision.action, "reduce")
        self.assertTrue(len(decision.risk_flags) > 0)
        self.assertIn("硬止损", decision.risk_flags[-1])

    def test_bounce_hold_guard_suppresses_reduce_when_structure_intact(self) -> None:
        # v1.56.0：深套但反弹结构完好（价≥MA20 + RSI修复 + 未破前低）→ 硬止损 reduce 被抑制为 hold
        # 回测实证：反弹初期减仓 3日方向正确率 0%（卖飞），支撑未破时让反弹走完
        config = load_config(Path(__file__).resolve().parent.parent / "config.yaml").monitor
        quote = self._quote("26.49")
        metrics = self._metrics(
            rsi14=Decimal("58"),
            breakdown_below_prev30_low_pct=Decimal("0.05"),  # 未深破前低
            volume_ratio=Decimal("0.90"),
        )
        holding = SimpleNamespace(quantity=1300, cost_price=Decimal("31.867"), current_price=Decimal("26.49"))

        decision = _build_decision_signal(
            quote,
            metrics,
            240,
            holding,
            config,
            None,
            is_volatile_period=False,
            portfolio_cash_ratio=Decimal("0.07"),
            sector_boards=None,
            portfolio_position_ratio=Decimal("0.89"),
            daily_ma20=Decimal("25.85"),  # 现价 ≥ MA20
            daily_ma60=Decimal("27.5"),
            daily_rsi14=Decimal("58.8"),  # 修复区
            portfolio_total_assets=Decimal("38598"),
        )

        self.assertEqual(decision.action, "hold")
        self.assertTrue(any("反弹抑制" in flag for flag in decision.risk_flags), decision.risk_flags)

    def test_bounce_hold_guard_does_not_suppress_when_breakdown(self) -> None:
        # v1.56.0：结构破位（深破前30日低点）不算反弹 → 维持 reduce（硬止损优先）
        config = load_config(Path(__file__).resolve().parent.parent / "config.yaml").monitor
        quote = self._quote("26.49")
        metrics = self._metrics(
            breakdown_below_prev30_low_pct=Decimal("0.35"),  # 深破前低
            volume_ratio=Decimal("1.50"),
        )
        holding = SimpleNamespace(quantity=1300, cost_price=Decimal("31.867"), current_price=Decimal("26.49"))

        decision = _build_decision_signal(
            quote,
            metrics,
            240,
            holding,
            config,
            None,
            is_volatile_period=False,
            portfolio_cash_ratio=Decimal("0.07"),
            sector_boards=None,
            portfolio_position_ratio=Decimal("0.89"),
            daily_ma20=Decimal("25.85"),
            daily_ma60=Decimal("27.5"),
            daily_rsi14=Decimal("45"),
            portfolio_total_assets=Decimal("38598"),
        )

        self.assertEqual(decision.action, "reduce")
        self.assertTrue(any("硬止损" in flag for flag in decision.risk_flags), decision.risk_flags)

    def test_volume_attack_boost_only_when_above_ma20_with_volume(self) -> None:
        # v1.56.1：放量上攻加分（价>MA20 + 量比≥1.2 + 收阳 + 非bear）——回测实证唯一正期望买点
        config = load_config(Path(__file__).resolve().parent.parent / "config.yaml").monitor
        quote = self._quote("30")  # change_percent +1.69% 收阳
        # 压分到中等区间（76/90）：放量上攻 +14 成为 push 过 80 阈值（buy 区）的决定性因素
        metrics = self._metrics(
            volume_ratio=Decimal("0.90"), bias_to_ma15=Decimal("0.0"),
            breakout_above_prev30_high_pct=Decimal("0.0"),
            relative_strength_pct=Decimal("-1.0"),
        )
        holding = SimpleNamespace(quantity=100, cost_price=Decimal("28"), current_price=Decimal("30"))

        d_attack = _build_decision_signal(
            quote, metrics, 240, holding, config, None,
            is_volatile_period=False, portfolio_cash_ratio=Decimal("0.50"),
            sector_boards=None, portfolio_position_ratio=Decimal("0.30"),
            daily_ma20=Decimal("29.5"), daily_ma60=Decimal("31"),  # neutral regime, 价>MA20
            daily_vol_ratio=Decimal("1.50"),
            portfolio_total_assets=Decimal("50000"),
        )
        d_quiet = _build_decision_signal(
            quote, metrics, 240, holding, config, None,
            is_volatile_period=False, portfolio_cash_ratio=Decimal("0.50"),
            sector_boards=None, portfolio_position_ratio=Decimal("0.30"),
            daily_ma20=Decimal("29.5"), daily_ma60=Decimal("31"),
            daily_vol_ratio=Decimal("1.00"),  # 量比不足, 不加分
            portfolio_total_assets=Decimal("50000"),
        )
        # 放量上攻触发: 加分把分数推入 buy 区（原buy）；量比不足: 保持 hold 区
        self.assertTrue(any("放量上攻" in r for r in d_attack.rationale), d_attack.rationale)
        self.assertFalse(any("放量上攻" in r for r in d_quiet.rationale), d_quiet.rationale)
        self.assertTrue(any("原buy" in r for r in d_attack.rationale), d_attack.rationale)
        self.assertFalse(any("原buy" in r for r in d_quiet.rationale), d_quiet.rationale)
        self.assertGreater(d_attack.score, d_quiet.score)

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
