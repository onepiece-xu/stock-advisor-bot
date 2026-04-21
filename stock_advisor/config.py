from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .models import StockRef
from .trading_plan import load_triggers


@dataclass(slots=True)
class Thresholds:
    daily_change_pct: float
    average_bias_pct: float
    abnormal_step_pct: float
    abnormal_range_pct: float


@dataclass(slots=True)
class DecisionThresholds:
    buy_score: float
    hold_score: float
    reduce_score: float


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
    benchmark: StockRef | None
    schedule: ScheduleConfig
    history_size: int
    thresholds: Thresholds
    decision_thresholds: DecisionThresholds
    provider_settings: ProviderSettings
    notification: NotificationConfig
    stop_loss_pct: float


@dataclass(slots=True)
class PortfolioConfig:
    data_dir: Path


@dataclass(slots=True)
class StorageConfig:
    sqlite_path: Path


@dataclass(slots=True)
class TradingPlanConfig:
    path: Path


@dataclass(slots=True)
class ReviewConfig:
    enabled: bool
    auto_notify: bool
    send_after_hour: int
    send_after_minute: int
    data_dir: Path


@dataclass(slots=True)
class AppConfig:
    monitor: MonitorConfig
    portfolio: PortfolioConfig
    storage: StorageConfig
    trading_plan: TradingPlanConfig
    review: ReviewConfig
    feishu_bot: FeishuBotConfig


class ConfigValidationError(RuntimeError):
    pass


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    monitor_raw = raw.get("monitor", {})
    schedule_raw = monitor_raw.get("schedule", {})
    thresholds_raw = monitor_raw.get("signal", {}).get("thresholds", {})
    provider_settings_raw = monitor_raw.get("provider_settings", {})
    benchmark_raw = monitor_raw.get("benchmark", {})
    notification_raw = monitor_raw.get("notification", {})
    dedup_raw = notification_raw.get("dedup", {})
    feishu_raw = notification_raw.get("feishu", {})
    trading_plan_raw = raw.get("trading_plan", {})
    review_raw = raw.get("review", {})
    bot_raw = raw.get("feishu_bot", {})

    stocks = [
        StockRef(exchange=item["exchange"], code=str(item["code"]))
        for item in monitor_raw.get("stocks", [])
    ]

    return AppConfig(
        monitor=MonitorConfig(
            provider=monitor_raw.get("provider", "eastmoney_minute"),
            stocks=stocks,
            benchmark=(
                None
                if benchmark_raw.get("enabled", True) is False
                else StockRef(
                    exchange=str(benchmark_raw.get("exchange", "sh")),
                    code=str(benchmark_raw.get("code", "000001")),
                )
            ),
            schedule=ScheduleConfig(
                enabled=bool(schedule_raw.get("enabled", True)),
                run_on_startup=bool(schedule_raw.get("run_on_startup", True)),
                fixed_delay_seconds=int(schedule_raw.get("fixed_delay_seconds", 300)),
                restrict_to_trading_session=bool(schedule_raw.get("restrict_to_trading_session", True)),
                market_time_zone=str(schedule_raw.get("market_time_zone", "Asia/Shanghai")),
            ),
            history_size=int(monitor_raw.get("signal", {}).get("history_size", 480)),
            thresholds=Thresholds(
                daily_change_pct=float(thresholds_raw.get("daily_change_pct", 2.0)),
                average_bias_pct=float(thresholds_raw.get("average_bias_pct", 1.0)),
                abnormal_step_pct=float(thresholds_raw.get("abnormal_step_pct", 1.5)),
                abnormal_range_pct=float(thresholds_raw.get("abnormal_range_pct", 3.0)),
            ),
            decision_thresholds=DecisionThresholds(
                buy_score=float(monitor_raw.get("signal", {}).get("decision_thresholds", {}).get("buy_score", 78.0)),
                hold_score=float(monitor_raw.get("signal", {}).get("decision_thresholds", {}).get("hold_score", 58.0)),
                reduce_score=float(monitor_raw.get("signal", {}).get("decision_thresholds", {}).get("reduce_score", 38.0)),
            ),
            provider_settings=ProviderSettings(
                request_timeout_ms=int(provider_settings_raw.get("request_timeout_ms", 4000)),
                tencent_base_url=provider_settings_raw.get("tencent", {}).get("base_url", "https://qt.gtimg.cn/q="),
            ),
            stop_loss_pct=float(monitor_raw.get("signal", {}).get("stop_loss_pct", 7.0)),
            notification=NotificationConfig(
                notify_on_neutral=bool(notification_raw.get("notify_on_neutral", False)),
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
        trading_plan=TradingPlanConfig(
            path=(config_path.parent / trading_plan_raw.get("path", "trading-plan.json")).resolve()
        ),
        review=ReviewConfig(
            enabled=bool(review_raw.get("enabled", True)),
            auto_notify=bool(review_raw.get("auto_notify", True)),
            send_after_hour=int(review_raw.get("send_after_hour", 15)),
            send_after_minute=int(review_raw.get("send_after_minute", 10)),
            data_dir=(config_path.parent / review_raw.get("data_dir", "data/reviews")).resolve(),
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


def validate_config(path: str | Path) -> list[str]:
    errors: list[str] = []
    config = load_config(path)

    if not config.monitor.stocks:
        errors.append("monitor.stocks 不能为空")

    if config.monitor.provider not in {"tencent", "eastmoney_minute"}:
        errors.append("monitor.provider 仅支持 tencent 或 eastmoney_minute")

    if config.monitor.history_size < 2:
        errors.append("monitor.signal.history_size 至少应为 2")
    elif config.monitor.provider == "eastmoney_minute" and config.monitor.history_size < 240:
        errors.append(
            "monitor.signal.history_size 在 eastmoney_minute 模式下至少应为 240；"
            "小于 240 会导致分钟均线和量能窗口失真，24 仅覆盖约 12 分钟"
        )

    thresholds = config.monitor.decision_thresholds
    if not (0 <= thresholds.reduce_score < thresholds.hold_score < thresholds.buy_score <= 100):
        errors.append("monitor.signal.decision_thresholds 必须满足 0 <= reduce_score < hold_score < buy_score <= 100")

    if not (0 < config.monitor.stop_loss_pct <= 50):
        errors.append("monitor.signal.stop_loss_pct 必须在 0-50 之间（单位：%，默认 7.0）")

    if config.monitor.schedule.fixed_delay_seconds <= 0:
        errors.append("monitor.schedule.fixed_delay_seconds 必须大于 0")

    if config.monitor.provider_settings.request_timeout_ms <= 0:
        errors.append("monitor.provider_settings.request_timeout_ms 必须大于 0")

    if config.monitor.notification.dedup.cooldown_minutes < 0:
        errors.append("monitor.notification.dedup.cooldown_minutes 不能小于 0")

    if config.monitor.notification.feishu.delivery_mode not in {"webhook", "direct_dm"}:
        errors.append("monitor.notification.feishu.delivery_mode 仅支持 webhook 或 direct_dm")

    if config.monitor.notification.feishu.enabled:
        if config.monitor.notification.feishu.delivery_mode == "webhook" and not config.monitor.notification.feishu.webhook_url:
            errors.append("开启 webhook 通知时必须填写 monitor.notification.feishu.webhook_url")

    if config.review.send_after_hour < 0 or config.review.send_after_hour > 23:
        errors.append("review.send_after_hour 必须在 0-23 之间")

    if config.review.send_after_minute < 0 or config.review.send_after_minute > 59:
        errors.append("review.send_after_minute 必须在 0-59 之间")

    if config.feishu_bot.enabled:
        if not config.feishu_bot.app_id:
            errors.append("开启 feishu_bot 时必须填写 feishu_bot.app_id")
        if not config.feishu_bot.app_secret:
            errors.append("开启 feishu_bot 时必须填写 feishu_bot.app_secret")
        if config.feishu_bot.listen_port <= 0 or config.feishu_bot.listen_port > 65535:
            errors.append("feishu_bot.listen_port 必须在 1-65535 之间")

    try:
        triggers = load_triggers(config.trading_plan.path)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"trading_plan.path 无法读取: {exc}")
        return errors

    for code, trigger in triggers.items():
        if trigger.action not in {"sell", "hold", "buy"}:
            errors.append(f"trading plan {code} 的 action 仅支持 sell/hold/buy")
        if trigger.quantity <= 0:
            errors.append(f"trading plan {code} 的 quantity 必须大于 0")
        if trigger.price_min > trigger.price_max:
            errors.append(f"trading plan {code} 的 priceMin 不能大于 priceMax")
        if trigger.fallback_price <= 0:
            errors.append(f"trading plan {code} 的 fallbackPrice 必须大于 0")

    return errors


def require_valid_config(path: str | Path) -> AppConfig:
    errors = validate_config(path)
    if errors:
        raise ConfigValidationError("配置校验失败:\n- " + "\n- ".join(errors))
    return load_config(path)
