from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .advice import build_action_candidates, render_action_candidates
from .config import MonitorConfig
from .models import DecisionSignal, ObservationMetrics, ObservationResult, StockQuote


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
    metrics = ObservationMetrics(
        avg3=avg3,
        avg6=avg6,
        bias_to_avg3=bias_to_avg3,
        bias_to_avg6=bias_to_avg6,
        step_change_pct=step_change_pct,
        recent_range_pct=recent_range_pct,
        intraday_amplitude_pct=current.intraday_amplitude_percent,
    )
    decision = _build_decision_signal(current, metrics, len(history))
    title = f"{current.code} {current.name} 行情观察"
    message = _build_message(current, metrics, decision, observations)
    should_notify = has_daily_change_alert or has_step_alert or has_range_alert
    signal_level = "ALERT" if should_notify else ("INFO" if has_non_neutral else "NEUTRAL")
    return ObservationResult(
        title=title,
        message=message,
        observations=observations,
        should_notify=should_notify,
        signal_level=signal_level,
        metrics=metrics,
        decision=decision,
    )


def _build_message(
    current: StockQuote,
    metrics: ObservationMetrics,
    decision: DecisionSignal,
    observations: list[str],
) -> str:
    lines = [
        f"标的：{current.code} {current.name}",
        f"数据源：{current.provider}",
        f"时间：{current.quote_time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"现价：{_format_price(current.current_price)}",
        f"昨收：{_format_price(current.previous_close)}",
        f"涨跌幅：{_format_percent(current.change_percent)}",
        f"近3次均价：{_format_price(metrics.avg3)}",
        f"近6次均价：{_format_price(metrics.avg6)}",
        f"单次采样变化：{_format_percent(metrics.step_change_pct)}",
        f"近6次区间波动：{_format_percent(metrics.recent_range_pct)}",
        f"振幅：{_format_percent(current.intraday_amplitude_percent)}",
        f"成交量(股)：{current.volume_shares.quantize(Decimal('1'), rounding=ROUND_HALF_UP)}",
        f"成交额(元)：{current.turnover_yuan.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}",
        "",
        "【AI辅助决策】",
        f"动作：{decision.action}",
        f"直接建议：{decision.trade_advice}",
        f"建议仓位：{decision.trade_size_hint}",
        f"入场/处理：{decision.entry_note}",
        f"评分：{decision.score.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}/100",
        f"置信度：{decision.confidence}",
        f"状态：{decision.regime}",
        f"理由：{'；'.join(decision.rationale)}",
        f"风险：{'；'.join(decision.risk_flags) if decision.risk_flags else '暂无显著风险标记'}",
        "",
        "【观察】",
    ]
    return "\n".join(lines + observations)


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


def _build_decision_signal(current: StockQuote, metrics: ObservationMetrics, sample_size: int) -> DecisionSignal:
    score = Decimal("50")
    rationale: list[str] = []
    risk_flags: list[str] = []

    if current.change_percent >= Decimal("1.50"):
        score += Decimal("12")
        rationale.append("当日涨幅偏强，短线动能在增强")
    elif current.change_percent <= Decimal("-1.50"):
        score -= Decimal("14")
        rationale.append("当日回撤偏大，短线抛压仍在释放")

    if metrics.bias_to_avg3 >= Decimal("0.80"):
        score += Decimal("10")
        rationale.append("现价站上近3次均价，短线节奏转强")
    elif metrics.bias_to_avg3 <= Decimal("-0.80"):
        score -= Decimal("10")
        rationale.append("现价跌破近3次均价，短线修复不足")

    if metrics.bias_to_avg6 >= Decimal("1.20"):
        score += Decimal("12")
        rationale.append("现价高于近6次均价，短周期趋势占优")
    elif metrics.bias_to_avg6 <= Decimal("-1.20"):
        score -= Decimal("12")
        rationale.append("现价低于近6次均价，趋势仍偏弱")

    if metrics.step_change_pct >= Decimal("1.00"):
        score += Decimal("8")
        rationale.append("最近一次采样继续抬升，延续性较好")
    elif metrics.step_change_pct <= Decimal("-1.00"):
        score -= Decimal("8")
        rationale.append("最近一次采样继续回落，修复被打断")

    if metrics.recent_range_pct >= Decimal("4.50"):
        score -= Decimal("8")
        risk_flags.append("近6次采样波动过大，节奏不稳定")

    if metrics.intraday_amplitude_pct >= Decimal("5.00"):
        score -= Decimal("6")
        risk_flags.append("日内振幅偏大，追价性价比低")

    if current.current_price >= current.open_price > 0:
        score += Decimal("4")
        rationale.append("现价守在开盘价上方，盘中承接尚可")
    elif current.open_price > 0 and current.current_price < current.open_price:
        score -= Decimal("4")
        rationale.append("现价落在开盘价下方，资金承接偏弱")

    if not rationale:
        rationale.append("当前多空信号较均衡，继续观察后续样本更稳妥")

    score = max(Decimal("0"), min(score, Decimal("100")))
    action = _decision_action(score)
    trade_advice, trade_size_hint, entry_note = _trade_plan(action, current, metrics)
    return DecisionSignal(
        action=action,
        score=score.quantize(Decimal("0.01")),
        confidence=_confidence_level(score, sample_size),
        regime=_market_regime(current, metrics),
        rationale=rationale,
        risk_flags=risk_flags,
        trade_advice=trade_advice,
        trade_size_hint=trade_size_hint,
        entry_note=entry_note,
    )


def _confidence_level(score: Decimal, sample_size: int) -> str:
    edge = abs(score - Decimal("50"))
    if sample_size < 3:
        return "low"
    if sample_size >= 6 and edge >= Decimal("18"):
        return "high"
    if sample_size >= 4 and edge >= Decimal("10"):
        return "medium"
    return "low"


def _market_regime(current: StockQuote, metrics: ObservationMetrics) -> str:
    if current.change_percent >= 0 and metrics.bias_to_avg3 >= 0 and metrics.bias_to_avg6 >= 0:
        return "momentum"
    if current.change_percent < 0 and metrics.bias_to_avg3 < 0 and metrics.bias_to_avg6 < 0:
        return "drawdown"
    if metrics.recent_range_pct >= Decimal("4.50"):
        return "volatile"
    return "range"


def _decision_action(score: Decimal) -> str:
    if score >= Decimal("72"):
        return "buy"
    if score >= Decimal("55"):
        return "hold"
    if score >= Decimal("35"):
        return "reduce"
    return "avoid"


def _trade_plan(action: str, current: StockQuote, metrics: ObservationMetrics) -> tuple[str, str, str]:
    if action == "buy":
        return (
            "可以小仓位试买，不追高",
            "建议 10%-15% 试探仓",
            f"优先等回踩不破 {_format_price(current.previous_close)} 或再次转强时再进",
        )
    if action == "hold":
        return (
            "继续持有观察，不主动加仓",
            "维持原仓，不新增",
            f"看能否稳在近3次均价 {_format_price(metrics.avg3)} 上方",
        )
    if action == "reduce":
        return (
            "逢反弹减仓一部分",
            "建议减 15%-30%",
            f"若反弹但不能有效站稳近6次均价 {_format_price(metrics.avg6)}，优先减仓",
        )
    return (
        "暂时不要买，已有仓位也别急着补",
        "禁止加仓",
        f"至少等价格重新回到近3次均价 {_format_price(metrics.avg3)} 上方再看",
    )


def _format_price(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def _format_percent(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"
