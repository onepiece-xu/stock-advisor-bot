from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .advice import build_action_candidates, render_action_candidates
from .config import MonitorConfig
from .models import DecisionSignal, ObservationMetrics, ObservationResult, PortfolioHolding, StockQuote, TradingHabitProfile
from .news import fetch_stock_news, render_news_lines


def analyze_quotes(
    history: list[StockQuote],
    monitor_config: MonitorConfig,
    *,
    include_news: bool = True,
    portfolio_holding: PortfolioHolding | None = None,
    benchmark_history: list[StockQuote] | None = None,
    trading_habit_profile: TradingHabitProfile | None = None,
    market_advance_ratio: Decimal = Decimal("0"),
    hot_stock_rank: int = 0,
    is_volatile_period: bool = False,
    portfolio_cash_ratio: Decimal | None = None,
    sector_boards: list[dict] | None = None,
    portfolio_position_ratio: Decimal | None = None,
) -> ObservationResult:
    current = history[-1]
    observations: list[str] = []

    ma5 = _average_of_last(history, 5)
    ma15 = _average_of_last(history, 15)
    ma60 = _average_of_last(history, 60)
    ma240 = _average_of_last(history, 240)
    rsi14 = _rsi_of_last(history, 14)
    bias_to_ma15 = _percent_diff(current.current_price, ma15)
    bias_to_ma60 = _percent_diff(current.current_price, ma60)
    step_change_pct = _percent_diff(current.current_price, history[-2].current_price) if len(history) >= 2 else Decimal("0")
    gap_pct = _percent_diff(current.open_price, current.previous_close) if current.previous_close > 0 and current.open_price > 0 else Decimal("0")
    recent_range_pct = _range_percent_of_last(history, 30)
    (
        minute_volume_shares,
        avg5_minute_volume_shares,
        avg30_minute_volume_shares,
        volume_ratio,
        volume_ratio_30,
        volume_trend_ratio,
    ) = _volume_profile(history)
    breakout_above_prev30_high_pct, breakdown_below_prev30_low_pct = _price_structure_profile(history)
    macd_line, macd_signal, macd_histogram, macd_prev_histogram = _macd_of_last(history)

    benchmark_change_pct = Decimal("0")
    relative_strength_pct = Decimal("0")
    benchmark_quote = benchmark_history[-1] if benchmark_history else None
    if benchmark_quote is not None:
        benchmark_change_pct = benchmark_quote.change_percent
        relative_strength_pct = (current.change_percent - benchmark_quote.change_percent).quantize(Decimal("0.01"))

    thresholds = monitor_config.thresholds
    has_daily_change_alert = abs(current.change_percent) >= Decimal(str(thresholds.daily_change_pct))
    has_ma15_bias_info = len(history) >= 15 and abs(bias_to_ma15) >= Decimal(str(thresholds.average_bias_pct))
    has_ma60_bias_info = len(history) >= 60 and abs(bias_to_ma60) >= Decimal(str(thresholds.average_bias_pct))
    has_step_alert = len(history) >= 2 and abs(step_change_pct) >= Decimal(str(thresholds.abnormal_step_pct))
    has_range_alert = len(history) >= 15 and recent_range_pct >= Decimal(str(thresholds.abnormal_range_pct))
    has_volume_alert = any(
        (
            volume_ratio >= Decimal("2.00"),
            volume_ratio_30 >= Decimal("1.50"),
            current.change_percent > 0 and volume_ratio <= Decimal("0.80"),
            current.change_percent > 0 and volume_trend_ratio <= Decimal("0.80"),
            breakout_above_prev30_high_pct >= Decimal("0.20"),
            breakdown_below_prev30_low_pct >= Decimal("0.20"),
        )
    )
    has_relative_strength_alert = benchmark_quote is not None and abs(relative_strength_pct) >= Decimal("1.50")
    has_rsi_alert = rsi14 >= Decimal("78") or rsi14 <= Decimal("28")
    _macd_golden_cross = macd_histogram > 0 and macd_prev_histogram <= 0
    _macd_death_cross = macd_histogram < 0 and macd_prev_histogram >= 0
    has_macd_alert = _macd_golden_cross or _macd_death_cross

    if has_daily_change_alert:
        direction = "偏强" if current.change_percent >= 0 else "偏弱"
        observations.append(f"观察：当日涨跌幅 {_format_percent(current.change_percent)}，日内表现{direction}。")
    if has_ma15_bias_info:
        direction = "高于" if bias_to_ma15 >= 0 else "低于"
        observations.append(f"观察：现价较 MA15 {direction} {_format_percent(abs(bias_to_ma15))}，短线节奏已偏离均值。")
    if has_ma60_bias_info:
        direction = "高于" if bias_to_ma60 >= 0 else "低于"
        observations.append(f"观察：现价较 MA60 {direction} {_format_percent(abs(bias_to_ma60))}，1 小时级别强弱已出现偏移。")
    if has_step_alert:
        direction = "拉升" if step_change_pct >= 0 else "回落"
        observations.append(f"观察：最近 1 分钟出现{direction} {_format_percent(abs(step_change_pct))} 的异动。")
    if gap_pct >= Decimal("1.50"):
        observations.append(f"观察：今日高开 {_format_percent(gap_pct)}，开盘即有溢价，需留意高开低走风险。")
    elif gap_pct <= Decimal("-1.50"):
        observations.append(f"观察：今日低开 {_format_percent(gap_pct)}，开盘承压，关注能否回补缺口。")
    if has_range_alert:
        observations.append(f"观察：近 30 分钟区间波动达到 {_format_percent(recent_range_pct)}，日内节奏偏剧烈。")
    if volume_ratio >= Decimal("5.00"):
        direction = "上行" if current.change_percent >= 0 else "下行"
        observations.append(f"观察：当前分钟量比 {_format_ratio(volume_ratio)}（5倍+），主力资金突刺{direction}，注意方向确认。")
    elif volume_ratio >= Decimal("2.00") and current.change_percent >= 0:
        observations.append(f"观察：当前分钟量比 {_format_ratio(volume_ratio)}，属于放量上行。")
    elif volume_ratio <= Decimal("0.80") and current.change_percent > 0:
        observations.append(f"观察：当前分钟量比仅 {_format_ratio(volume_ratio)}，上涨缺少量能配合。")
    elif volume_ratio >= Decimal("1.80") and current.change_percent < 0:
        observations.append(f"观察：当前分钟量比 {_format_ratio(volume_ratio)}，下跌伴随放量，抛压偏重。")
    if breakout_above_prev30_high_pct >= Decimal("0.20") and volume_ratio_30 >= Decimal("1.50"):
        observations.append(
            f"观察：现价放量突破近 30 分钟前高 {_format_percent(breakout_above_prev30_high_pct)}，突破质量较好。"
        )
    elif breakout_above_prev30_high_pct >= Decimal("0.20") and volume_ratio_30 < Decimal("1.10"):
        observations.append(
            f"观察：现价创近 30 分钟新高，但 30 分钟量比仅 {_format_ratio(volume_ratio_30)}，假突破风险偏高。"
        )
    if breakdown_below_prev30_low_pct >= Decimal("0.20") and volume_ratio_30 >= Decimal("1.50"):
        observations.append(
            f"观察：现价放量跌破近 30 分钟前低 {_format_percent(breakdown_below_prev30_low_pct)}，承接明显转弱。"
        )
    if volume_trend_ratio >= Decimal("1.20") and current.change_percent > 0:
        observations.append(
            f"观察：近 5 分钟均量已升至 30 分钟基线的 {_format_ratio(volume_trend_ratio)} 倍，资金参与在增强。"
        )
    elif volume_trend_ratio <= Decimal("0.80") and current.change_percent > 0:
        observations.append(
            f"观察：近 5 分钟均量仅为 30 分钟基线的 {_format_ratio(volume_trend_ratio)} 倍，反弹动能不足。"
        )
    if rsi14 >= Decimal("78"):
        observations.append(f"观察：RSI14 为 {_format_ratio(rsi14)}，已接近超买区，追涨性价比偏低。")
    elif rsi14 <= Decimal("28"):
        observations.append(f"观察：RSI14 为 {_format_ratio(rsi14)}，已接近超卖区，留意是否出现止跌修复。")
    if _macd_golden_cross:
        observations.append(f"观察：MACD 金叉（柱线由负转正），短线多头信号，关注量能配合。")
    elif _macd_death_cross:
        observations.append(f"观察：MACD 死叉（柱线由正转负），短线空头信号，谨慎追多。")
    elif macd_histogram > 0 and macd_histogram > macd_prev_histogram:
        observations.append(f"观察：MACD 红柱走宽（{_format_ratio(macd_histogram)}），多头动能持续增强。")
    elif macd_histogram < 0 and macd_histogram < macd_prev_histogram:
        observations.append(f"观察：MACD 绿柱走宽（{_format_ratio(macd_histogram)}），空头动能持续释放。")
    if hot_stock_rank == 1:
        observations.append("观察：今日涨幅全市场第一，属于市场热点龙头。")
    elif hot_stock_rank <= 5:
        observations.append(f"观察：今日涨幅全市场前 5（第 {hot_stock_rank} 名），属于强势热点股。")
    elif hot_stock_rank <= 20:
        observations.append(f"观察：今日涨幅全市场前 20（第 {hot_stock_rank} 名），市场关注度偏高。")
    elif hot_stock_rank <= 50:
        observations.append(f"观察：今日涨幅进入全市场前 50（第 {hot_stock_rank} 名）。")
    if sector_boards:
        for board in sector_boards:
            if board.get("leader_code") == current.code:
                observations.append(
                    f"观察：本股为今日【{board['name']}】板块龙头（涨幅 {board.get('change_percent', 0):+.2f}%），板块效应加持。"
                )
    if market_advance_ratio >= Decimal("0.65"):
        observations.append(f"观察：全市场上涨家数占比 {_format_percent(market_advance_ratio * 100)}，人气偏强。")
    elif Decimal("0") < market_advance_ratio <= Decimal("0.35"):
        observations.append(f"观察：全市场上涨家数占比仅 {_format_percent(market_advance_ratio * 100)}，市场人气偏弱。")
    if benchmark_quote is not None:
        observations.append(
            f"观察：基准 {benchmark_quote.name} {_format_percent(benchmark_change_pct)}，个股相对强弱 {_format_percent(relative_strength_pct)}。"
        )
    if len(history) < monitor_config.history_size:
        observations.append(f"观察：当前仅拿到 {len(history)}/{monitor_config.history_size} 根分钟样本，长周期判断可靠性下降。")

    has_non_neutral = bool(observations)
    if not observations:
        observations.append("观察：当前未触发明显信号，先看量价是否继续配合。")

    observations.extend(render_action_candidates(build_action_candidates(current)))
    if include_news:
        observations.extend(render_news_lines(fetch_stock_news(current)))

    metrics = ObservationMetrics(
        ma5=ma5,
        ma15=ma15,
        ma60=ma60,
        ma240=ma240,
        rsi14=rsi14,
        bias_to_ma15=bias_to_ma15,
        bias_to_ma60=bias_to_ma60,
        step_change_pct=step_change_pct,
        recent_range_pct=recent_range_pct,
        intraday_amplitude_pct=current.intraday_amplitude_percent,
        minute_volume_shares=minute_volume_shares,
        avg5_minute_volume_shares=avg5_minute_volume_shares,
        avg30_minute_volume_shares=avg30_minute_volume_shares,
        volume_ratio=volume_ratio,
        volume_ratio_30=volume_ratio_30,
        volume_trend_ratio=volume_trend_ratio,
        breakout_above_prev30_high_pct=breakout_above_prev30_high_pct,
        breakdown_below_prev30_low_pct=breakdown_below_prev30_low_pct,
        benchmark_change_pct=benchmark_change_pct,
        relative_strength_pct=relative_strength_pct,
        macd_line=macd_line,
        macd_signal=macd_signal,
        macd_histogram=macd_histogram,
        macd_prev_histogram=macd_prev_histogram,
        market_advance_ratio=market_advance_ratio,
        hot_stock_rank=hot_stock_rank,
    )
    history_count = len(history)
    decision = _build_decision_signal(
        current,
        metrics,
        history_count,
        portfolio_holding,
        monitor_config,
        trading_habit_profile,
        is_volatile_period=is_volatile_period,
        portfolio_cash_ratio=portfolio_cash_ratio,
        sector_boards=sector_boards,
        portfolio_position_ratio=portfolio_position_ratio,
    )
    sparkline = _render_sparkline(history)
    title = f"{current.code} {current.name} 行情观察"
    message = _build_message(
        current,
        metrics,
        decision,
        observations,
        benchmark_quote=benchmark_quote,
        history_count=history_count,
        expected_history_count=monitor_config.history_size,
        sparkline=sparkline,
    )
    should_notify = any(
        (
            has_daily_change_alert,
            has_step_alert,
            has_range_alert,
            has_volume_alert,
            has_relative_strength_alert,
            has_rsi_alert,
            has_macd_alert,
        )
    )
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
    *,
    benchmark_quote: StockQuote | None,
    history_count: int,
    expected_history_count: int,
    sparkline: str = "",
) -> str:
    benchmark_line = (
        f"基准：{benchmark_quote.name} {_format_percent(metrics.benchmark_change_pct)} | 相对强弱 {_format_percent(metrics.relative_strength_pct)}"
        if benchmark_quote is not None
        else "基准：N/A"
    )
    lines = [
        f"标的：{current.code} {current.name}",
        f"数据源：{current.provider}",
        f"时间：{current.quote_time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"现价：{_format_price(current.current_price)}",
        f"昨收：{_format_price(current.previous_close)}",
        f"涨跌幅：{_format_percent(current.change_percent)}",
        f"走势（近{min(history_count, 30)}分）：{sparkline}",
        f"样本窗口：{history_count}/{expected_history_count}",
        f"MA5：{_format_price(metrics.ma5)}",
        f"MA15：{_format_price(metrics.ma15)}",
        f"MA60：{_format_price(metrics.ma60)}",
        f"MA240：{_format_price(metrics.ma240)}",
        f"RSI14：{_format_ratio(metrics.rsi14)}",
        f"MACD：{_format_ratio(metrics.macd_line)} | 信号线：{_format_ratio(metrics.macd_signal)} | 柱线：{_format_ratio(metrics.macd_histogram)}",
        f"相对 MA15：{_format_percent(metrics.bias_to_ma15)}",
        f"相对 MA60：{_format_percent(metrics.bias_to_ma60)}",
        f"分钟量：{_format_volume(metrics.minute_volume_shares)}",
        f"5分均量：{_format_volume(metrics.avg5_minute_volume_shares)}",
        f"30分均量：{_format_volume(metrics.avg30_minute_volume_shares)}",
        f"量比：5分 {_format_ratio(metrics.volume_ratio)} | 30分 {_format_ratio(metrics.volume_ratio_30)}",
        f"量能趋势：{_format_ratio(metrics.volume_trend_ratio)}",
        benchmark_line,
        f"市场人气：上涨占比 {_format_percent(metrics.market_advance_ratio * 100)}" + (f" | 个股热度排名 #{metrics.hot_stock_rank}" if metrics.hot_stock_rank > 0 else ""),
        f"近30分波动：{_format_percent(metrics.recent_range_pct)}",
        f"振幅：{_format_percent(metrics.intraday_amplitude_pct)}",
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


def _average_decimal(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return (sum(values, Decimal("0")) / Decimal(len(values))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _safe_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= 0:
        return Decimal("1.00")
    return (numerator / denominator).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _range_percent_of_last(history: list[StockQuote], count: int) -> Decimal:
    quotes = history[-count:] if len(history) >= count else history
    if not quotes:
        return Decimal("0")
    prices = [quote.current_price for quote in quotes]
    avg = _average_of_last(quotes, len(quotes))
    if avg <= 0:
        return Decimal("0")
    return (((max(prices) - min(prices)) / avg) * Decimal("100")).quantize(Decimal("0.01"))


def _volume_profile(history: list[StockQuote]) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]:
    minute_volumes: list[Decimal] = []
    previous: StockQuote | None = None
    for quote in history:
        if previous is None or previous.quote_time.date() != quote.quote_time.date():
            minute_volumes.append(max(quote.volume_shares, Decimal("0")))
        else:
            minute_volumes.append(max(quote.volume_shares - previous.volume_shares, Decimal("0")))
        previous = quote
    current_minute = minute_volumes[-1] if minute_volumes else Decimal("0")
    reference5 = minute_volumes[-6:-1] if len(minute_volumes) >= 6 else minute_volumes[:-1]
    if not reference5:
        reference5 = minute_volumes[-5:] if minute_volumes else [Decimal("0")]
    reference30 = minute_volumes[-31:-1] if len(minute_volumes) >= 31 else minute_volumes[:-1]
    if not reference30:
        reference30 = minute_volumes[-30:] if minute_volumes else [Decimal("0")]

    avg5 = _average_decimal(reference5)
    avg30 = _average_decimal(reference30)
    ratio5 = _safe_ratio(current_minute, avg5)
    ratio30 = _safe_ratio(current_minute, avg30)
    trend_ratio = _safe_ratio(avg5, avg30)
    return (
        current_minute.quantize(Decimal("1"), rounding=ROUND_HALF_UP),
        avg5,
        avg30,
        ratio5,
        ratio30,
        trend_ratio,
    )


def _price_structure_profile(history: list[StockQuote]) -> tuple[Decimal, Decimal]:
    previous_quotes = history[-31:-1] if len(history) >= 31 else history[:-1]
    if not previous_quotes:
        return Decimal("0.00"), Decimal("0.00")

    previous_high = max(quote.current_price for quote in previous_quotes)
    previous_low = min(quote.current_price for quote in previous_quotes)
    current_price = history[-1].current_price

    breakout_pct = Decimal("0.00")
    if previous_high > 0 and current_price > previous_high:
        breakout_pct = (((current_price - previous_high) / previous_high) * Decimal("100")).quantize(Decimal("0.01"))

    breakdown_pct = Decimal("0.00")
    if previous_low > 0 and current_price < previous_low:
        breakdown_pct = (((previous_low - current_price) / previous_low) * Decimal("100")).quantize(Decimal("0.01"))

    return breakout_pct, breakdown_pct


def _rsi_of_last(history: list[StockQuote], period: int) -> Decimal:
    if len(history) < 2:
        return Decimal("50.00")
    deltas: list[Decimal] = []
    for previous, current in zip(history[:-1], history[1:]):
        deltas.append(current.current_price - previous.current_price)
    window = deltas[-period:] if len(deltas) >= period else deltas
    if not window:
        return Decimal("50.00")
    gains = sum((delta for delta in window if delta > 0), Decimal("0"))
    losses = sum((-delta for delta in window if delta < 0), Decimal("0"))
    avg_gain = gains / Decimal(len(window))
    avg_loss = losses / Decimal(len(window))
    if avg_loss == 0:
        return Decimal("100.00") if avg_gain > 0 else Decimal("50.00")
    rs = avg_gain / avg_loss
    rsi = Decimal("100") - (Decimal("100") / (Decimal("1") + rs))
    return rsi.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _macd_of_last(history: list[StockQuote]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Returns (macd_line, signal_line, histogram, prev_histogram). EMA(5,13,5) tuned for minute bars."""
    if len(history) < 2:
        return Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")
    prices = [q.current_price for q in history]
    k5  = Decimal("2") / Decimal("6")   # EMA(5)  — 5-min momentum
    k13 = Decimal("2") / Decimal("14")  # EMA(13) — 13-min trend
    k5s = Decimal("2") / Decimal("6")   # Signal EMA(5)
    ema5  = prices[0]
    ema13 = prices[0]
    macd_series: list[Decimal] = []
    for price in prices[1:]:
        ema5  = price * k5  + ema5  * (Decimal("1") - k5)
        ema13 = price * k13 + ema13 * (Decimal("1") - k13)
        macd_series.append(ema5 - ema13)
    if not macd_series:
        return Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0")
    signal = macd_series[0]
    prev_signal = signal
    for m in macd_series[1:]:
        prev_signal = signal
        signal = m * k5s + signal * (Decimal("1") - k5s)
    macd_line = macd_series[-1]
    prev_macd = macd_series[-2] if len(macd_series) >= 2 else macd_series[-1]
    histogram = macd_line - signal
    prev_histogram = prev_macd - prev_signal
    q = Decimal("0.00001")
    return macd_line.quantize(q), signal.quantize(q), histogram.quantize(q), prev_histogram.quantize(q)


def _percent_diff(current: Decimal, base: Decimal) -> Decimal:
    if base <= 0:
        return Decimal("0")
    return (((current - base) / base) * Decimal("100")).quantize(Decimal("0.01"))


def _build_decision_signal(
    current: StockQuote,
    metrics: ObservationMetrics,
    sample_size: int,
    portfolio_holding: PortfolioHolding | None,
    monitor_config: MonitorConfig,
    trading_habit_profile: TradingHabitProfile | None,
    *,
    is_volatile_period: bool = False,
    portfolio_cash_ratio: Decimal | None = None,
    sector_boards: list[dict] | None = None,
    portfolio_position_ratio: Decimal | None = None,
) -> DecisionSignal:
    score = Decimal("50")
    rationale: list[str] = []
    risk_flags: list[str] = []

    if current.change_percent >= Decimal("2.00"):
        score += Decimal("8")
        rationale.append("当日涨幅偏强，价格已经进入强势区")
    elif current.change_percent <= Decimal("-2.00"):
        score -= Decimal("10")
        rationale.append("当日回撤明显，短线承压较大")

    if metrics.bias_to_ma15 >= Decimal("0.60"):
        score += Decimal("8")
        rationale.append("现价站上 MA15，短线趋势偏强")
    elif metrics.bias_to_ma15 <= Decimal("-0.80"):
        score -= Decimal("10")
        rationale.append("现价跌破 MA15，短线修复不足")

    if metrics.bias_to_ma60 >= Decimal("1.20"):
        score += Decimal("12")
        rationale.append("现价高于 MA60，小时级别趋势占优")
    elif metrics.bias_to_ma60 <= Decimal("-1.20"):
        score -= Decimal("12")
        rationale.append("现价低于 MA60，小时级别仍偏弱")

    if metrics.ma240 > 0 and current.current_price >= metrics.ma240:
        score += Decimal("4")
        rationale.append("现价维持在 MA240 上方，全天结构尚可")
    elif metrics.ma240 > 0 and current.current_price < metrics.ma240:
        score -= Decimal("4")
        rationale.append("现价跌回 MA240 下方，全天结构偏弱")

    if metrics.step_change_pct >= Decimal("1.00"):
        score += Decimal("4")
        rationale.append("最近 1 分钟继续抬升，短线延续性尚可")
    elif metrics.step_change_pct <= Decimal("-1.00"):
        score -= Decimal("5")
        rationale.append("最近 1 分钟回落较快，拉升持续性不足")

    if metrics.volume_ratio >= Decimal("5.00"):
        if current.change_percent >= Decimal("0.30"):
            score += Decimal("12")
            rationale.append(f"量能突刺上行（量比 {_format_ratio(metrics.volume_ratio)}），主力资金强势介入")
        elif current.change_percent <= Decimal("-0.30"):
            score -= Decimal("12")
            risk_flags.append(f"量能突刺下行（量比 {_format_ratio(metrics.volume_ratio)}），主力快速出货信号")
        else:
            score -= Decimal("4")
            risk_flags.append(f"量能突刺但价格滞涨（量比 {_format_ratio(metrics.volume_ratio)}），多空分歧加剧")
    elif metrics.volume_ratio >= Decimal("2.00") and current.change_percent >= Decimal("0.30"):
        score += Decimal("10")
        rationale.append("放量上行，量价配合较好")
    elif metrics.volume_ratio <= Decimal("0.80") and current.change_percent > 0:
        score -= Decimal("8")
        risk_flags.append("上涨缩量，冲高持续性存疑")
    elif metrics.volume_ratio >= Decimal("1.80") and current.change_percent < 0:
        score -= Decimal("10")
        risk_flags.append("下跌放量，抛压仍在释放")

    if metrics.volume_trend_ratio >= Decimal("1.25") and current.change_percent > 0:
        score += Decimal("6")
        rationale.append("近 5 分钟均量高于 30 分钟基线，短线参与度正在增强")
    elif metrics.volume_trend_ratio <= Decimal("0.80") and current.change_percent > 0:
        score -= Decimal("6")
        risk_flags.append("近 5 分钟量能弱于 30 分钟基线，反弹持续性不足")
    elif metrics.volume_trend_ratio >= Decimal("1.20") and current.change_percent < 0 and metrics.volume_ratio_30 >= Decimal("1.20"):
        score -= Decimal("6")
        risk_flags.append("下跌过程中近 5 分钟成交继续放大，资金流出仍偏主动")

    if metrics.breakout_above_prev30_high_pct >= Decimal("0.20") and metrics.volume_ratio_30 >= Decimal("1.50"):
        score += Decimal("12")
        rationale.append("放量突破近 30 分钟前高，属于更有效的日内突破")
    elif metrics.breakout_above_prev30_high_pct >= Decimal("0.20") and metrics.volume_ratio_30 >= Decimal("1.15"):
        score += Decimal("5")
        rationale.append("价格突破近 30 分钟前高，但量能只到中等强度")
    elif metrics.breakout_above_prev30_high_pct >= Decimal("0.20"):
        score -= Decimal("8")
        risk_flags.append("价格突破前高但 30 分钟量能未同步放大，假突破风险较高")

    if metrics.breakdown_below_prev30_low_pct >= Decimal("0.20") and metrics.volume_ratio_30 >= Decimal("1.50"):
        score -= Decimal("12")
        risk_flags.append("放量跌破近 30 分钟前低，承接明显不足")
    elif metrics.breakdown_below_prev30_low_pct >= Decimal("0.20"):
        score -= Decimal("6")
        risk_flags.append("跌破近 30 分钟前低，短线结构已被破坏")

    if metrics.rsi14 >= Decimal("80"):
        score -= Decimal("12")
        risk_flags.append("RSI14 过高，追高风险显著")
    elif metrics.rsi14 >= Decimal("72"):
        score -= Decimal("6")
        risk_flags.append("RSI14 偏高，继续上冲空间受限")
    elif metrics.rsi14 <= Decimal("25"):
        score -= Decimal("3")
        risk_flags.append("RSI14 进入超卖区，先等止跌确认")
    elif metrics.rsi14 <= Decimal("32") and metrics.relative_strength_pct > 0:
        score += Decimal("3")
        rationale.append("RSI14 低位且相对指数不弱，存在修复窗口")

    macd_golden_cross = metrics.macd_histogram > 0 and metrics.macd_prev_histogram <= 0
    macd_death_cross = metrics.macd_histogram < 0 and metrics.macd_prev_histogram >= 0
    if metrics.macd_line > 0:
        score += Decimal("4")
        rationale.append("MACD 线在零轴上方，处于多头区域")
    elif metrics.macd_line < 0:
        score -= Decimal("4")
        rationale.append("MACD 线在零轴下方，处于空头区域")
    if macd_golden_cross:
        score += Decimal("8")
        rationale.append("MACD 金叉，短线由空转多")
    elif macd_death_cross:
        score -= Decimal("8")
        risk_flags.append("MACD 死叉，短线由多转空")
    elif metrics.macd_histogram > 0 and metrics.macd_histogram > metrics.macd_prev_histogram:
        score += Decimal("5")
        rationale.append("MACD 红柱扩大，多头动能持续增强")
    elif metrics.macd_histogram < 0 and metrics.macd_histogram < metrics.macd_prev_histogram:
        score -= Decimal("5")
        risk_flags.append("MACD 绿柱扩大，空头动能持续释放")

    if metrics.benchmark_change_pct <= Decimal("-0.80") and metrics.relative_strength_pct >= Decimal("1.00"):
        score += Decimal("8")
        rationale.append("大盘偏弱时仍能跑赢指数，属于逆势偏强")
    elif metrics.benchmark_change_pct >= Decimal("0.50") and metrics.relative_strength_pct <= Decimal("-1.00"):
        score -= Decimal("8")
        rationale.append("大盘并不弱但个股明显跑输，承接偏弱")
    elif metrics.benchmark_change_pct <= Decimal("-1.00") and current.change_percent < 0:
        score -= Decimal("6")
        risk_flags.append("大盘走弱时个股同步回撤，逆势属性不足")

    if metrics.market_advance_ratio >= Decimal("0.65"):
        score += Decimal("4")
        rationale.append("市场人气偏强，上涨家数占比 65% 以上")
    elif Decimal("0") < metrics.market_advance_ratio <= Decimal("0.35"):
        score -= Decimal("4")
        risk_flags.append("市场人气偏弱，上涨家数不足 35%")

    if metrics.hot_stock_rank == 1:
        score += Decimal("8")
        rationale.append("今日涨幅全市场第一，市场热点龙头")
    elif metrics.hot_stock_rank <= 5:
        score += Decimal("6")
        rationale.append(f"今日涨幅全市场前 5（第 {metrics.hot_stock_rank} 名），强势热点")
    elif metrics.hot_stock_rank <= 20:
        score += Decimal("4")
        rationale.append(f"进入今日全市场前 20 强势股（第 {metrics.hot_stock_rank} 名）")
    elif metrics.hot_stock_rank <= 50:
        score += Decimal("2")
        rationale.append(f"进入今日全市场前 50 强势股（第 {metrics.hot_stock_rank} 名）")

    if sector_boards:
        for board in sector_boards:
            if board.get("leader_code") == current.code:
                score += Decimal("5")
                rationale.append(f"板块龙头（{board['name']} {board.get('change_percent', 0):+.2f}%），获得板块效应加持")
                break

    if portfolio_cash_ratio is not None:
        if portfolio_cash_ratio < Decimal("0.15"):
            score -= Decimal("5")
            risk_flags.append(f"组合现金比例偏低（{_format_percent(portfolio_cash_ratio * 100)}），加仓空间有限")
        elif portfolio_cash_ratio >= Decimal("0.40"):
            score += Decimal("2")
            rationale.append(f"组合现金充裕（{_format_percent(portfolio_cash_ratio * 100)}），具备加仓条件")

    if portfolio_position_ratio is not None:
        if portfolio_position_ratio >= Decimal("0.60"):
            score -= Decimal("8")
            risk_flags.append(f"单票持仓集中度过高（占总资产 {_format_percent(portfolio_position_ratio * 100)}），建议分散风险")
        elif portfolio_position_ratio >= Decimal("0.50"):
            score -= Decimal("4")
            risk_flags.append(f"单票持仓集中度偏高（占总资产 {_format_percent(portfolio_position_ratio * 100)}），注意控制仓位上限")

    if is_volatile_period:
        score -= Decimal("5")
        risk_flags.append("当前处于开盘/收盘波动期，信号可靠性偏低，建议等市场稳定后再执行")

    if metrics.recent_range_pct >= Decimal("4.50"):
        score -= Decimal("5")
        risk_flags.append("近 30 分钟波动偏大，追单性价比低")
    if metrics.intraday_amplitude_pct >= Decimal("5.00"):
        score -= Decimal("4")
        risk_flags.append("日内振幅偏大，容易来回扫损")

    if current.current_price >= current.open_price > 0:
        score += Decimal("3")
        rationale.append("现价守在开盘价上方，盘中承接尚可")
    elif current.open_price > 0 and current.current_price < current.open_price:
        score -= Decimal("3")
        rationale.append("现价落在开盘价下方，盘中资金承接偏弱")

    gap_pct = _percent_diff(current.open_price, current.previous_close) if current.previous_close > 0 and current.open_price > 0 else Decimal("0")
    if gap_pct >= Decimal("2.50"):
        score -= Decimal("5")
        risk_flags.append(f"高开 {_format_percent(gap_pct)}，追高风险偏大，注意高开低走")
    elif gap_pct >= Decimal("1.50"):
        score -= Decimal("2")
        risk_flags.append(f"小幅高开 {_format_percent(gap_pct)}，需确认能否持续")
    elif gap_pct <= Decimal("-2.50"):
        score -= Decimal("6")
        risk_flags.append(f"大幅低开 {_format_percent(gap_pct)}，空头主导，谨慎抄底")
    elif gap_pct <= Decimal("-1.50"):
        score -= Decimal("3")
        risk_flags.append(f"低开 {_format_percent(gap_pct)}，开盘承压，观望为主")

    if not rationale:
        rationale.append("当前多空信号仍偏均衡，继续等待更明确的量价配合")

    regime = _market_regime(current, metrics)
    if regime == "distribution":
        score -= Decimal("6")
        risk_flags.append("量价背离（高位放量滞涨），警惕主力派发")
    elif regime == "recovery":
        score += Decimal("4")
        rationale.append("超卖后出现止跌反弹迹象，关注量能确认")

    score = max(Decimal("0"), min(score, Decimal("100")))
    action = _decision_action(score, monitor_config, metrics.benchmark_change_pct)
    trade_advice, trade_size_hint, entry_note = _trade_plan(
        action,
        score,
        current,
        metrics,
        portfolio_holding,
        trading_habit_profile,
    )
    return DecisionSignal(
        action=action,
        score=score.quantize(Decimal("0.01")),
        confidence=_confidence_level(score, sample_size),
        regime=regime,
        rationale=rationale,
        risk_flags=risk_flags,
        trade_advice=trade_advice,
        trade_size_hint=trade_size_hint,
        entry_note=entry_note,
    )


def _confidence_level(score: Decimal, sample_size: int) -> str:
    edge = abs(score - Decimal("50"))
    if sample_size < 60:
        return "low"
    if sample_size >= 240 and edge >= Decimal("18"):
        return "high"
    if sample_size >= 120 and edge >= Decimal("10"):
        return "medium"
    return "low"


def _market_regime(current: StockQuote, metrics: ObservationMetrics) -> str:
    # Distribution: heavy volume near highs but price stalling — possible top-side supply
    near_high = metrics.bias_to_ma60 >= Decimal("0.50")
    heavy_vol = metrics.volume_ratio_30 >= Decimal("1.50")
    stalling = metrics.step_change_pct <= Decimal("0.10") or metrics.rsi14 >= Decimal("72")
    if near_high and heavy_vol and stalling and current.change_percent <= Decimal("1.50"):
        return "distribution"

    # Recovery: oversold + nascent bounce (step up or MACD golden cross)
    oversold = metrics.rsi14 <= Decimal("32")
    bouncing = metrics.step_change_pct >= Decimal("0.20") or (
        metrics.macd_histogram > 0 and metrics.macd_prev_histogram <= 0
    )
    if oversold and bouncing:
        return "recovery"

    if current.change_percent >= 0 and metrics.bias_to_ma15 >= 0 and metrics.bias_to_ma60 >= 0 and metrics.relative_strength_pct >= 0:
        return "momentum"
    if current.change_percent < 0 and metrics.bias_to_ma15 < 0 and metrics.bias_to_ma60 < 0 and metrics.relative_strength_pct <= 0:
        return "drawdown"
    if metrics.recent_range_pct >= Decimal("4.50"):
        return "volatile"
    return "range"


def _decision_action(
    score: Decimal,
    monitor_config: MonitorConfig,
    benchmark_change_pct: Decimal = Decimal("0"),
) -> str:
    thresholds = monitor_config.decision_thresholds
    buy = Decimal(str(thresholds.buy_score))
    hold = Decimal(str(thresholds.hold_score))
    reduce = Decimal(str(thresholds.reduce_score))
    if benchmark_change_pct >= Decimal("1.0"):
        shift = Decimal("-4")   # bull day: lower bar, more willing to buy
    elif benchmark_change_pct <= Decimal("-1.0"):
        shift = Decimal("4")    # bear day: raise bar, more selective
    else:
        shift = Decimal("0")
    if score >= buy + shift:
        return "buy"
    if score >= hold + shift:
        return "hold"
    if score >= reduce + shift:
        return "reduce"
    return "avoid"


def _trade_plan(
    action: str,
    score: Decimal,
    current: StockQuote,
    metrics: ObservationMetrics,
    portfolio_holding: PortfolioHolding | None,
    trading_habit_profile: TradingHabitProfile | None,
) -> tuple[str, str, str]:
    habit_note = _habit_note(trading_habit_profile)
    if action == "buy":
        buy_qty = _recommended_buy_quantity(score, portfolio_holding, trading_habit_profile)
        return (
            f"先买入 {buy_qty} 股试单，不追高{habit_note}",
            f"买入 {buy_qty} 股",
            f"优先等回踩不破 MA15 {_format_price(metrics.ma15)} 或再度放量转强时再进",
        )
    if action == "hold":
        hold_qty = portfolio_holding.quantity if portfolio_holding is not None and portfolio_holding.quantity > 0 else 0
        hold_hint = f"继续持有 {hold_qty} 股" if hold_qty > 0 else "维持当前仓位，不新增"
        return (
            f"继续持有观察，不主动加仓{habit_note}",
            hold_hint,
            f"观察能否持续站稳 MA15 {_format_price(metrics.ma15)} 与 MA60 {_format_price(metrics.ma60)}",
        )
    if action == "reduce":
        reduce_qty = _recommended_reduce_quantity(score, portfolio_holding, trading_habit_profile)
        return (
            f"反弹时先减仓 {reduce_qty} 股{habit_note}",
            f"减仓 {reduce_qty} 股",
            f"若反弹仍站不稳 MA60 {_format_price(metrics.ma60)}，优先卖出 {reduce_qty} 股",
        )
    avoid_qty = _recommended_avoid_quantity(portfolio_holding, trading_habit_profile)
    return (
        f"暂时不要买；若已有持仓，先减仓 {avoid_qty} 股{habit_note}",
        f"减仓 {avoid_qty} 股，禁止加仓",
        f"至少等价格重新回到 MA15 {_format_price(metrics.ma15)} 上方，且量比回升后再看",
    )


def _recommended_buy_quantity(
    score: Decimal,
    portfolio_holding: PortfolioHolding | None,
    trading_habit_profile: TradingHabitProfile | None,
) -> int:
    if trading_habit_profile is not None and trading_habit_profile.sample_count >= 3:
        if portfolio_holding is not None and portfolio_holding.quantity > 0:
            return trading_habit_profile.preferred_add_lot
        return trading_habit_profile.preferred_buy_lot
    if portfolio_holding is not None and portfolio_holding.quantity > 0:
        return 100
    return 200 if score >= Decimal("88") else 100


def _recommended_reduce_quantity(
    score: Decimal,
    portfolio_holding: PortfolioHolding | None,
    trading_habit_profile: TradingHabitProfile | None,
) -> int:
    if portfolio_holding is None or portfolio_holding.quantity <= 0:
        return 100
    quantity = portfolio_holding.quantity
    if quantity <= 100:
        return quantity
    if score <= Decimal("32"):
        ratio = Decimal("0.50")
    elif score <= Decimal("45"):
        ratio = Decimal("0.30")
    else:
        ratio = Decimal("0.20")
    if trading_habit_profile is not None and trading_habit_profile.sample_count >= 3:
        learned_ratio = trading_habit_profile.preferred_reduce_ratio
        ratio = ((ratio * Decimal("0.60")) + (learned_ratio * Decimal("0.40"))).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    return _round_to_sellable_lot(quantity, ratio)


def _recommended_avoid_quantity(
    portfolio_holding: PortfolioHolding | None,
    trading_habit_profile: TradingHabitProfile | None,
) -> int:
    if portfolio_holding is None or portfolio_holding.quantity <= 0:
        if trading_habit_profile is not None and trading_habit_profile.sample_count >= 3:
            return trading_habit_profile.preferred_buy_lot
        return 100
    quantity = portfolio_holding.quantity
    if quantity <= 200:
        return quantity
    ratio = Decimal("0.50")
    if trading_habit_profile is not None and trading_habit_profile.sample_count >= 3:
        ratio = max(ratio, trading_habit_profile.preferred_reduce_ratio)
    return _round_to_sellable_lot(quantity, ratio)


def _round_to_sellable_lot(quantity: int, ratio: Decimal) -> int:
    raw = (Decimal(quantity) * ratio).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    target = int(raw)
    if quantity < 100:
        return quantity
    rounded = (target // 100) * 100
    if rounded <= 0:
        return min(100, quantity)
    return min(rounded, quantity)


def _habit_note(trading_habit_profile: TradingHabitProfile | None) -> str:
    if trading_habit_profile is None or trading_habit_profile.sample_count < 3:
        return ""
    return "（已按你的历史成交习惯校准）"


_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _render_sparkline(history: list[StockQuote], count: int = 30) -> str:
    quotes = history[-count:] if len(history) >= count else history
    prices = [q.current_price for q in quotes]
    if not prices:
        return ""
    min_p, max_p = min(prices), max(prices)
    if max_p == min_p:
        return _SPARK_CHARS[3] * len(prices)
    n = len(_SPARK_CHARS) - 1
    return "".join(_SPARK_CHARS[int((p - min_p) / (max_p - min_p) * n)] for p in prices)


def _format_price(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def _format_percent(value: Decimal) -> str:
    scaled = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    prefix = "+" if scaled > 0 else ""
    return f"{prefix}{scaled}%"


def _format_ratio(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _format_volume(value: Decimal) -> str:
    return str(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
