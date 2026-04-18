from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .advice import build_action_candidates, render_action_candidates
from .config import MonitorConfig
from .models import ObservationMetrics, ObservationResult, StockQuote


def analyze_quotes(history: list[StockQuote], monitor_config: MonitorConfig) -> ObservationResult:
    current = history[-1]
    observations: list[str] = []

    avg3 = _average_of_last(history, 3)
    avg6 = _average_of_last(history, 6)
    bias_to_avg3 = _percent_diff(current.current_price, avg3)
    bias_to_avg6 = _percent_diff(current.current_price, avg6)
    step_change_pct = _percent_diff(current.current_price, history[-2].current_price) if len(history) >= 2 else Decimal("0")
    recent_range_pct = _range_percent_of_last(history, 6)

    thresholds = monitor_config.thresholds
    has_daily_change_alert = abs(current.change_percent) >= Decimal(str(thresholds.daily_change_pct))
    has_avg3_bias_info = len(history) >= 3 and abs(bias_to_avg3) >= Decimal(str(thresholds.average_bias_pct))
    has_avg6_bias_info = len(history) >= 6 and abs(bias_to_avg6) >= Decimal(str(thresholds.average_bias_pct))
    has_step_alert = len(history) >= 2 and abs(step_change_pct) >= Decimal(str(thresholds.abnormal_step_pct))
    has_range_alert = len(history) >= 6 and recent_range_pct >= Decimal(str(thresholds.abnormal_range_pct))

    if has_daily_change_alert:
        direction = "偏强" if current.change_percent >= 0 else "偏弱"
        observations.append(f"观察：当日涨跌幅 {_format_percent(current.change_percent)}，日内表现{direction}，建议结合成交额和板块联动继续跟踪。仅供参考，不构成投资建议。")
    if has_avg3_bias_info:
        direction = "高于" if bias_to_avg3 >= 0 else "低于"
        observations.append(f"观察：当前价较近 3 次均价{direction} {_format_percent(abs(bias_to_avg3))}，短线节奏有变化，留意后续延续性。仅供参考，不构成投资建议。")
    if has_avg6_bias_info:
        direction = "高于" if bias_to_avg6 >= 0 else "低于"
        observations.append(f"观察：当前价较近 6 次均价{direction} {_format_percent(abs(bias_to_avg6))}，短周期趋势正在偏移。仅供参考，不构成投资建议。")
    if has_step_alert:
        direction = "拉升" if step_change_pct >= 0 else "回落"
        observations.append(f"观察：单次采样出现{direction} {_format_percent(abs(step_change_pct))} 的短时异动，建议关注消息面和量能变化。仅供参考，不构成投资建议。")
    if has_range_alert:
        observations.append(f"观察：近 6 次采样区间波动达到 {_format_percent(recent_range_pct)}，属于异常波动，需注意节奏切换风险。仅供参考，不构成投资建议。")

    has_non_neutral = bool(observations)
    if not observations:
        observations.append("观察：当前未触发明显信号，建议继续跟踪价格、短期均价和成交额变化。仅供参考，不构成投资建议。")

    observations.extend(render_action_candidates(build_action_candidates(current)))
    title = f"{current.code} {current.name} 行情观察"
    message = _build_message(current, avg3, avg6, step_change_pct, recent_range_pct, observations)
    should_notify = has_daily_change_alert or has_step_alert or has_range_alert
    signal_level = "ALERT" if should_notify else ("INFO" if has_non_neutral else "NEUTRAL")
    metrics = ObservationMetrics(
        avg3=avg3,
        avg6=avg6,
        bias_to_avg3=bias_to_avg3,
        bias_to_avg6=bias_to_avg6,
        step_change_pct=step_change_pct,
        recent_range_pct=recent_range_pct,
        intraday_amplitude_pct=current.intraday_amplitude_percent,
    )
    return ObservationResult(
        title=title,
        message=message,
        observations=observations,
        should_notify=should_notify,
        signal_level=signal_level,
        metrics=metrics,
    )


def _build_message(current: StockQuote, avg3: Decimal, avg6: Decimal, step_change_pct: Decimal, recent_range_pct: Decimal, observations: list[str]) -> str:
    metrics = [
        f"标的：{current.code} {current.name}",
        f"数据源：{current.provider}",
        f"时间：{current.quote_time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"现价：{_format_price(current.current_price)}",
        f"昨收：{_format_price(current.previous_close)}",
        f"涨跌幅：{_format_percent(current.change_percent)}",
        f"近3次均价：{_format_price(avg3)}",
        f"近6次均价：{_format_price(avg6)}",
        f"单次采样变化：{_format_percent(step_change_pct)}",
        f"近6次区间波动：{_format_percent(recent_range_pct)}",
        f"振幅：{_format_percent(current.intraday_amplitude_percent)}",
        f"成交量(股)：{current.volume_shares.quantize(Decimal('1'), rounding=ROUND_HALF_UP)}",
        f"成交额(元)：{current.turnover_yuan.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}",
        "",
    ]
    return "\n".join(metrics + observations)


def _average_of_last(history: list[StockQuote], count: int) -> Decimal:
    quotes = history[-count:] if len(history) >= count else history
    if not quotes:
        return Decimal("0")
    total = sum((quote.current_price for quote in quotes), Decimal("0"))
    return (total / Decimal(len(quotes))).quantize(Decimal("0.0001"))


def _range_percent_of_last(history: list[StockQuote], count: int) -> Decimal:
    quotes = history[-count:] if len(history) >= count else history
    if not quotes:
        return Decimal("0")
    prices = [quote.current_price for quote in quotes]
    avg = _average_of_last(quotes, len(quotes))
    if avg <= 0:
        return Decimal("0")
    return (((max(prices) - min(prices)) / avg) * Decimal("100")).quantize(Decimal("0.01"))


def _percent_diff(current: Decimal, base: Decimal) -> Decimal:
    if base <= 0:
        return Decimal("0")
    return (((current - base) / base) * Decimal("100")).quantize(Decimal("0.01"))


def _format_price(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def _format_percent(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"
