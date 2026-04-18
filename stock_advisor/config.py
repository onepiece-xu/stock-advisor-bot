from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .models import StockRef


@dataclass(slots=True)
class Thresholds:
    daily_change_pct: float
    average_bias_pct: float
    abnormal_step_pct: float
    abnormal_range_pct: float


@dataclass(slots=True)
class FeishuConfig:
    enabled: bool
    webhook_url: str
    delivery_mode: str


@dataclass(slots=True)
class FeishuBotConfig:
    enabled: bool
    app_id: str
    app_secret: str
    verification_token: str
    listen_host: str
    listen_port: int
    allowed_chat_ids: list[str]


@dataclass(slots=True)
class DedupConfig:
    enabled: bool
    cooldown_minutes: int


@dataclass(slots=True)
class NotificationConfig:
    notify_on_neutral: bool
    dedup: DedupConfig
    feishu: FeishuConfig


@dataclass(slots=True)
class ProviderSettings:
    request_timeout_ms: int
    tencent_base_url: str


@dataclass(slots=True)
class ScheduleConfig:
    enabled: bool
    run_on_startup: bool
    fixed_delay_seconds: int
    restrict_to_trading_session: bool
    market_time_zone: str


@dataclass(slots=True)
class MonitorConfig:
    provider: str
    stocks: list[StockRef]
    schedule: ScheduleConfig
    history_size: int
    thresholds: Thresholds
    provider_settings: ProviderSettings
    notification: NotificationConfig


@dataclass(slots=True)
class PortfolioConfig:
    data_dir: Path


@dataclass(slots=True)
class StorageConfig:
    sqlite_path: Path


@dataclass(slots=True)
class AppConfig:
    monitor: MonitorConfig
    portfolio: PortfolioConfig
    storage: StorageConfig
    feishu_bot: FeishuBotConfig


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    monitor_raw = raw.get("monitor", {})
    schedule_raw = monitor_raw.get("schedule", {})
    thresholds_raw = monitor_raw.get("signal", {}).get("thresholds", {})
    provider_settings_raw = monitor_raw.get("provider_settings", {})
    notification_raw = monitor_raw.get("notification", {})
    dedup_raw = notification_raw.get("dedup", {})
    feishu_raw = notification_raw.get("feishu", {})
    bot_raw = raw.get("feishu_bot", {})

    stocks = [
        StockRef(exchange=item["exchange"], code=str(item["code"]))
        for item in monitor_raw.get("stocks", [])
    ]

    return AppConfig(
        monitor=MonitorConfig(
            provider=monitor_raw.get("provider", "tencent"),
            stocks=stocks,
            schedule=ScheduleConfig(
                enabled=bool(schedule_raw.get("enabled", True)),
                run_on_startup=bool(schedule_raw.get("run_on_startup", True)),
                fixed_delay_seconds=int(schedule_raw.get("fixed_delay_seconds", 300)),
                restrict_to_trading_session=bool(schedule_raw.get("restrict_to_trading_session", True)),
                market_time_zone=str(schedule_raw.get("market_time_zone", "Asia/Shanghai")),
            ),
            history_size=int(monitor_raw.get("signal", {}).get("history_size", 24)),
            thresholds=Thresholds(
                daily_change_pct=float(thresholds_raw.get("daily_change_pct", 2.0)),
                average_bias_pct=float(thresholds_raw.get("average_bias_pct", 1.0)),
                abnormal_step_pct=float(thresholds_raw.get("abnormal_step_pct", 1.5)),
                abnormal_range_pct=float(thresholds_raw.get("abnormal_range_pct", 3.0)),
            ),
            provider_settings=ProviderSettings(
                request_timeout_ms=int(provider_settings_raw.get("request_timeout_ms", 4000)),
                tencent_base_url=provider_settings_raw.get("tencent", {}).get("base_url", "https://qt.gtimg.cn/q="),
            ),
            notification=NotificationConfig(
                notify_on_neutral=bool(notification_raw.get("notify_on_neutral", True)),
                dedup=DedupConfig(
                    enabled=bool(dedup_raw.get("enabled", True)),
                    cooldown_minutes=int(dedup_raw.get("cooldown_minutes", 30)),
                ),
                feishu=FeishuConfig(
                    enabled=bool(feishu_raw.get("enabled", False)),
                    webhook_url=str(feishu_raw.get("webhook_url", "")),
                    delivery_mode=str(feishu_raw.get("delivery_mode", "webhook")),
                ),
            ),
        ),
        portfolio=PortfolioConfig(
            data_dir=(config_path.parent / raw.get("portfolio", {}).get("data_dir", "data/portfolio")).resolve()
        ),
        storage=StorageConfig(
            sqlite_path=(config_path.parent / raw.get("storage", {}).get("sqlite_path", "data/market.db")).resolve()
        ),
        feishu_bot=FeishuBotConfig(
            enabled=bool(bot_raw.get("enabled", False)),
            app_id=str(bot_raw.get("app_id", "")),
            app_secret=str(bot_raw.get("app_secret", "")),
            verification_token=str(bot_raw.get("verification_token", "")),
            listen_host=str(bot_raw.get("listen_host", "0.0.0.0")),
            listen_port=int(bot_raw.get("listen_port", 8788)),
            allowed_chat_ids=[str(item) for item in bot_raw.get("allowed_chat_ids", [])],
        ),
    )
