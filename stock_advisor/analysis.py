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
    daily_closes: list[Decimal] | None = None,
    daily_volumes: list[Decimal] | None = None,
    portfolio_total_assets: Decimal | None = None,
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

    # Limit up/down detection
    is_limit_up = current.change_percent >= Decimal("9.5")
    is_limit_down = current.change_percent <= Decimal("-9.5")
    if is_limit_up:
        observations.append("⚠️ 涨停板：无法买入，持有者可等待开板或次日。")
    if is_limit_down:
        observations.append("⚠️ 跌停板：无法卖出，成交量可能为零，停止一切操作。")


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
    ma_align = _ma_alignment_score(current.current_price, ma5, ma15, ma60, ma240)
    if ma_align >= 4:
        observations.append("观察：均线多头排列完整（价格>MA5>MA15>MA60>MA240），中长线趋势结构最优。")
    elif ma_align <= -4:
        observations.append("观察：均线空头排列完整（价格<MA5<MA15<MA60<MA240），各周期均线均偏空。")

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
    daily_ma20 = _average_decimal_list(daily_closes, 20) if daily_closes else None
    daily_ma60 = _average_decimal_list(daily_closes, 60) if daily_closes else None
    daily_rsi14 = _daily_rsi(daily_closes, 14) if daily_closes and len(daily_closes) >= 15 else None
    daily_vol_ratio = _daily_volume_ratio(daily_volumes, 5) if daily_volumes and len(daily_volumes) >= 6 else None
    recent_ex_div = _detect_ex_dividend_gap(daily_closes) if daily_closes and len(daily_closes) >= 5 else False
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
        daily_ma20=daily_ma20,
        daily_ma60=daily_ma60,
        daily_rsi14=daily_rsi14,
        daily_vol_ratio=daily_vol_ratio,
        recent_ex_dividend=recent_ex_div,
        portfolio_total_assets=portfolio_total_assets,
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
        portfolio_holding=portfolio_holding,
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
    portfolio_holding: PortfolioHolding | None = None,
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
        f"均线排列：{_describe_ma_alignment(_ma_alignment_score(current.current_price, metrics.ma5, metrics.ma15, metrics.ma60, metrics.ma240))}",
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
        f"操作指令：{decision.trade_advice}",
        f"执行数量：{decision.trade_size_hint}",
        f"当前持仓：{portfolio_holding.quantity} 股" if portfolio_holding and portfolio_holding.quantity > 0 else "当前持仓：无",
        f"触发条件：{decision.entry_note}",
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
    """Safe division for ratio calculations — returns 0.00 on invalid denominator.

    Returns 0.00 instead of 1.00 to avoid masking data errors as "normal".
    Callers should check for 0.00 and treat it as "data unavailable".
    """
    if denominator <= 0:
        return Decimal("0.00")
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


def _confidence_level(score: Decimal, sample_size: int) -> str:
    """Determine confidence level based on score extremity and sample size."""
    edge = abs(score - Decimal("50"))
    if sample_size < 60:
        return "low"
    if sample_size >= 240 and edge >= Decimal("18"):
        return "high"
    if sample_size >= 120 and edge >= Decimal("14"):
        return "high"
    if sample_size >= 120 and edge >= Decimal("10"):
        return "medium"
    if edge >= Decimal("16"):
        return "medium"
    return "low"


def _market_regime(current: StockQuote, metrics: ObservationMetrics) -> str:
    """Detect intraday volume-price regime.
    
    Returns:
        "distribution" — volume-price divergence (量价背离)
        "recovery" — oversold bounce (超卖反弹)
        "neutral" — no clear regime
    """
    change = current.change_percent
    vol_ratio = metrics.volume_ratio
    rsi = metrics.rsi14
    
    # Distribution: price up but volume very low (涨不动), or price down with high volume (放量下跌)
    if (change > Decimal("0.5") and vol_ratio < Decimal("0.6")) or \
       (change < Decimal("-1.0") and vol_ratio > Decimal("1.5")):
        return "distribution"
    
    # Recovery: price up with decent volume from oversold RSI
    if change > Decimal("1.0") and rsi < Decimal("40") and vol_ratio > Decimal("0.8"):
        return "recovery"
    
    return "neutral"


def _decision_action(
    score: Decimal,
    monitor_config,
    benchmark_change_pct: Decimal = Decimal("0"),
    market_advance_ratio: Decimal = Decimal("0.5"),
) -> str:
    """Map a calibrated score to a trading action using configurable thresholds.

    Thresholds from config.yaml -> monitor.signal.decision_thresholds:
      buy_score (default 78), hold_score (default 58), reduce_score (default 38)
    """
    thresholds = monitor_config.decision_thresholds
    buy_thresh = Decimal(str(thresholds.buy_score))
    hold_thresh = Decimal(str(thresholds.hold_score))
    reduce_thresh = Decimal(str(thresholds.reduce_score))

    if score >= buy_thresh:
        return "buy"
    if score >= hold_thresh:
        return "hold"
    if score >= reduce_thresh:
        return "reduce"
    return "avoid"


def _apply_account_risk_guards(
    action: str,
    monitor_config,
    portfolio_holding,
    *,
    portfolio_cash_ratio: Decimal | None = None,
    portfolio_position_ratio: Decimal | None = None,
) -> tuple[str, list[str], list[str]]:
    """Apply account-level risk limits that override trade-level decisions.

    Returns (possibly_changed_action, rationale_lines, risk_flag_lines).
    These guards are hard limits — they override any buy signal regardless of score.
    """
    guard_rationales: list[str] = []
    guard_risk_flags: list[str] = []

    risk = getattr(monitor_config, "risk_controls", None)
    max_single_pct = Decimal(str(getattr(risk, "max_single_position_pct", 35) if risk else 35))
    max_total_pct = Decimal(str(getattr(risk, "max_total_position_pct", 85) if risk else 85))
    min_cash_pct = Decimal(str(getattr(risk, "min_cash_pct", 15) if risk else 15))

    # ── Deep loss: never buy more of a deep-loss position ──
    if portfolio_holding and action == "buy":
        pnl_pct = _safe_pnl_pct(portfolio_holding)
        if pnl_pct is None:
            # Conservative: cannot determine PnL — disallow buying
            action = "avoid"
            guard_rationales.append("无法计算持仓盈亏，禁止买入")
            guard_risk_flags.append("⚠️ 持仓缺少市价数据，禁止补仓")
        elif pnl_pct <= Decimal("-20"):
            action = "avoid"
            guard_rationales.append("深套股不补仓")
            guard_risk_flags.append(f"⚠️ 深套 {float(pnl_pct):+.1f}%，禁止买入")

    # ── Single position limit ──
    if portfolio_position_ratio is not None and action == "buy":
        if portfolio_position_ratio >= max_single_pct:
            action = "avoid"
            guard_rationales.append(f"单票仓位{float(portfolio_position_ratio):.0f}%≥上限{float(max_single_pct):.0f}%")

    # ── Total position limit ──
    if portfolio_cash_ratio is not None and action == "buy":
        total_pos = Decimal("100") - portfolio_cash_ratio * Decimal("100")
        if total_pos >= max_total_pct:
            action = "avoid"
            guard_rationales.append(f"总仓位{float(total_pos):.0f}%≥上限{float(max_total_pct):.0f}%")

    # ── Minimum cash reserve ──
    if portfolio_cash_ratio is not None and action == "buy":
        if portfolio_cash_ratio * Decimal("100") < min_cash_pct:
            action = "avoid"
            guard_rationales.append(f"现金{float(portfolio_cash_ratio*100):.0f}%低于{float(min_cash_pct):.0f}%安全线")

    return action, guard_rationales, guard_risk_flags


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
    daily_ma20: Decimal | None = None,
    daily_ma60: Decimal | None = None,
    daily_rsi14: Decimal | None = None,
    daily_vol_ratio: Decimal | None = None,
    recent_ex_dividend: bool = False,
    portfolio_total_assets: Decimal | None = None,
) -> DecisionSignal:
    """Two-tier scoring: daily regime → position context → minute fine-tuning.

    Tier 1 — Daily Regime (sets scoring gravity):
      - Bull: price > MA20 > MA60 → floor raised, sell suppressed
      - Bear: price < MA20 < MA60 → ceiling lowered, buy suppressed
      - Neutral: standard scoring

    Tier 2 — Position Context (modulates penalties):
      - Winner (>+5%): penalties halved, hold is default
      - Normal (-5% to +5%): standard
      - Loser (-5% to -20%): buy penalties increased
      - Deep loss (<-20%): buy banned, only reduce

    Tier 3 — Minute Signals (fine-tuning, only in neutral regime):
      - Ignored when daily regime is Bull or Bear
    """
    # ═══════════════════════════════════════════════════════════
    # PHASE 0: Regime & Context
    # ═══════════════════════════════════════════════════════════
    has_position = portfolio_holding is not None and portfolio_holding.quantity > 0
    holding_return_pct = _holding_return_percent(current, portfolio_holding)

    # ── Daily Regime ──
    if daily_ma20 is not None and daily_ma60 is not None and daily_ma20 > 0 and daily_ma60 > 0:
        if current.current_price > daily_ma20 > daily_ma60:
            daily_regime = "bull"
        elif current.current_price < daily_ma20 < daily_ma60:
            daily_regime = "bear"
        else:
            daily_regime = "neutral"
    else:
        daily_regime = "neutral"

    # ═══════════════════════════════════════════════════════════
    # PHASE 0: Initialize scoring + rationale (MUST come before multi-timeframe filter)
    # ═══════════════════════════════════════════════════════════
    score = Decimal("50")
    rationale: list[str] = []
    risk_flags: list[str] = []

    # ── Multi-Timeframe Filter (Weekly + Daily) ──
    try:
        from .multi_timeframe import multi_timeframe_filter
        mtf = multi_timeframe_filter(
            current.symbol, current.current_price, daily_ma20, daily_ma60
        )
        mtf_block_buy = mtf.get("block_buy", False)
        mtf_block_sell = mtf.get("block_sell", False)
        mtf_score_adjust = mtf.get("score_adjust", 0)
        mtf_desc = mtf.get("description", "")
        if mtf_score_adjust != 0:
            score += Decimal(str(mtf_score_adjust))
        if mtf_desc:
            rationale.append(f"多周期: {mtf_desc}")
    except Exception:
        mtf_block_buy = False
        mtf_block_sell = False
        mtf_score_adjust = 0

    # ── Position Context ──
    if holding_return_pct is None:
        position_ctx = "no_position"
    elif holding_return_pct >= Decimal("5"):
        position_ctx = "winner"
    elif holding_return_pct >= Decimal("-5"):
        position_ctx = "normal"
    elif holding_return_pct >= Decimal("-20"):
        position_ctx = "loser"
    else:
        position_ctx = "deep_loss"

    # ═══════════════════════════════════════════════════════════
    # PHASE 1: Daily Regime Scoring (foundation)
    # ═══════════════════════════════════════════════════════════

    # 1.1 Daily MA Structure (dominant signal)
    if daily_regime == "bull":
        score += Decimal("24")
        rationale.append("日线多头排列：现价>MA20>MA60，大趋势向上")
    elif daily_regime == "bear":
        score -= Decimal("30")
        risk_flags.append("日线空头排列：现价<MA20<MA60，大趋势偏弱")

    # 1.2 Daily MA20 Bias (penalized only outside bull regime)
    if daily_ma20 is not None and daily_ma20 > 0:
        daily_ma20_bias = _percent_diff(current.current_price, daily_ma20)
        if daily_ma20_bias <= Decimal("-5.00"):
            score += Decimal("15")
            rationale.append(f"日线深度低于MA20 {_format_percent(daily_ma20_bias)}，均值回归")
        elif daily_ma20_bias <= Decimal("-2.00"):
            score += Decimal("6")
            rationale.append(f"日线低于MA20 {_format_percent(daily_ma20_bias)}，修复窗口")
        elif daily_ma20_bias >= Decimal("5.00") and daily_regime != "bull":
            score -= Decimal("18")
            risk_flags.append(f"日线远高于MA20 {_format_percent(daily_ma20_bias)}，追高风险")
        elif daily_ma20_bias >= Decimal("3.00") and daily_regime != "bull":
            score -= Decimal("9")
            risk_flags.append(f"日线偏离MA20偏高 {_format_percent(daily_ma20_bias)}，等回踩")

    # 1.3 Daily RSI14
    if daily_rsi14 is not None:
        if daily_rsi14 <= Decimal("25"):
            score += Decimal("24")
            rationale.append(f"日线RSI深度超卖 {_format_ratio(daily_rsi14)}，反弹概率>70%")
        elif daily_rsi14 <= Decimal("32"):
            score += Decimal("12")
            rationale.append(f"日线RSI超卖区 {_format_ratio(daily_rsi14)}")
        elif daily_rsi14 >= Decimal("80"):
            score -= Decimal("30")
            risk_flags.append(f"日线RSI极度超买 {_format_ratio(daily_rsi14)}，追涨胜率<30%")
        elif daily_rsi14 >= Decimal("70"):
            score -= Decimal("15")
            risk_flags.append(f"日线RSI超买区 {_format_ratio(daily_rsi14)}")

    # 1.4 Daily Volume Ratio
    if daily_vol_ratio is not None:
        if daily_vol_ratio >= Decimal("2.00") and current.change_percent >= 0:
            score += Decimal("18")
            rationale.append(f"日线放量上涨（量比 {_format_ratio(daily_vol_ratio)}）")
        elif daily_vol_ratio >= Decimal("2.00") and current.change_percent < 0:
            score -= Decimal("24")
            risk_flags.append(f"日线放量下跌（量比 {_format_ratio(daily_vol_ratio)}），出货信号")
        elif daily_vol_ratio <= Decimal("0.50") and current.change_percent >= 0 and daily_regime != "bull":
            score -= Decimal("12")
            risk_flags.append(f"日线缩量上涨（量比 {_format_ratio(daily_vol_ratio)}），动能不足")
        elif daily_vol_ratio <= Decimal("0.50") and current.change_percent < 0:
            score += Decimal("6")
            rationale.append(f"日线缩量下跌（量比 {_format_ratio(daily_vol_ratio)}），抛压减轻")

    # ═══════════════════════════════════════════════════════════
    # PHASE 2: Minute Signals (neutral regime only)
    # ═══════════════════════════════════════════════════════════
    if daily_regime == "neutral":
        # MA biases
        if metrics.bias_to_ma15 >= Decimal("0.60"):
            score += Decimal("8"); rationale.append("站上MA15")
        elif metrics.bias_to_ma15 <= Decimal("-0.80"):
            score -= Decimal("10"); rationale.append("跌破MA15")
        if metrics.bias_to_ma60 >= Decimal("1.20"):
            score += Decimal("12"); rationale.append("高于MA60")
        elif metrics.bias_to_ma60 <= Decimal("-1.20"):
            score -= Decimal("12"); rationale.append("低于MA60")

        # MA alignment
        ma_align = _ma_alignment_score(current.current_price, metrics.ma5, metrics.ma15, metrics.ma60, metrics.ma240)
        daily_is_bullish = (daily_ma20 is not None and daily_ma60 is not None and current.current_price > daily_ma20 > daily_ma60)
        if ma_align >= 4:
            score += Decimal("4"); rationale.append("均线多头排列")
        elif ma_align == 3:
            score += Decimal("2")
        elif ma_align <= -4:
            if not daily_is_bullish:
                score -= Decimal("4"); risk_flags.append("均线空头排列")
        elif ma_align == -3:
            if not daily_is_bullish:
                score -= Decimal("2"); risk_flags.append("均线偏空")

        # Volume ratio
        if metrics.volume_ratio >= Decimal("5.00"):
            if current.change_percent >= Decimal("0.30"):
                score += Decimal("12")
            elif current.change_percent <= Decimal("-0.30"):
                score -= Decimal("12"); risk_flags.append("量能突刺下行")
        elif metrics.volume_ratio >= Decimal("2.00") and current.change_percent >= Decimal("0.30"):
            score += Decimal("10"); rationale.append("放量上行")
        elif metrics.volume_ratio <= Decimal("0.80") and current.change_percent > 0:
            if current.change_percent >= Decimal("0.50"):
                score -= Decimal("8"); risk_flags.append("上涨缩量")
            elif current.change_percent >= Decimal("0.20"):
                score -= Decimal("3")

        # Volume trend
        if metrics.volume_trend_ratio <= Decimal("0.80") and current.change_percent > 0:
            score -= Decimal("6"); risk_flags.append("近5分钟量能弱")

        # Breakout/breakdown
        if metrics.breakout_above_prev30_high_pct >= Decimal("0.20"):
            if metrics.volume_ratio_30 >= Decimal("1.50"):
                score += Decimal("12"); rationale.append("放量突破前高")
            elif metrics.volume_ratio_30 >= Decimal("1.15"):
                score += Decimal("5")
            else:
                score -= Decimal("8"); risk_flags.append("缩量假突破")
        if metrics.breakdown_below_prev30_low_pct >= Decimal("0.20"):
            score -= Decimal("12") if metrics.volume_ratio_30 >= Decimal("1.50") else Decimal("6")

        # Step change
        if metrics.step_change_pct >= Decimal("1.00"):
            score += Decimal("4")
        elif metrics.step_change_pct <= Decimal("-1.00"):
            score -= Decimal("5")

        # RSI
        if metrics.rsi14 >= Decimal("80"):
            score -= Decimal("12"); risk_flags.append("RSI14过高")
        elif metrics.rsi14 >= Decimal("72"):
            score -= Decimal("6")
        elif metrics.rsi14 <= Decimal("25"):
            score += Decimal("8"); rationale.append("RSI14超卖")
        elif metrics.rsi14 <= Decimal("32"):
            score += Decimal("4")

        # MACD
        if metrics.macd_line > 0: score += Decimal("4")
        elif metrics.macd_line < 0: score -= Decimal("4")
        if metrics.macd_histogram > 0 and metrics.macd_prev_histogram <= 0:
            score += Decimal("8"); rationale.append("MACD金叉")
        elif metrics.macd_histogram < 0 and metrics.macd_prev_histogram >= 0:
            score -= Decimal("8"); risk_flags.append("MACD死叉")

        # Volatility
        if is_volatile_period:
            score -= Decimal("5"); risk_flags.append("波动期信号可靠性低")
        if metrics.recent_range_pct >= Decimal("4.50"):
            score -= Decimal("5")
        if metrics.intraday_amplitude_pct >= Decimal("5.00"):
            score -= Decimal("4")

    # ═══════════════════════════════════════════════════════════
    # PHASE 3: Market Environment (all regimes)
    # ═══════════════════════════════════════════════════════════

    # 3.1 Market breadth (downweighted for bull+winner)
    if metrics.market_advance_ratio >= Decimal("0.65"):
        score += Decimal("4"); rationale.append("市场人气偏强")
    elif Decimal("0") < metrics.market_advance_ratio <= Decimal("0.35"):
        penalty = Decimal("2") if (daily_regime == "bull" and position_ctx in ("winner", "normal")) else Decimal("4")
        score -= penalty; risk_flags.append("市场人气偏弱，上涨不足35%")

    # 3.2 Relative strength
    if metrics.benchmark_change_pct <= Decimal("-0.80") and metrics.relative_strength_pct >= Decimal("1.00"):
        score += Decimal("8"); rationale.append("逆势跑赢大盘")
    elif metrics.benchmark_change_pct >= Decimal("0.50") and metrics.relative_strength_pct <= Decimal("-1.00"):
        score -= Decimal("8"); risk_flags.append("大盘不弱但个股跑输")

    # 3.3 Hot stock / sector leader
    if metrics.hot_stock_rank == 1:
        score += Decimal("8"); rationale.append("全市场涨幅第一")
    elif metrics.hot_stock_rank <= 5:
        score += Decimal("6")
    if sector_boards:
        for board in sector_boards:
            if board.get("leader_code") == current.code:
                score += Decimal("5"); rationale.append(f"板块龙头({board['name']})")
                break

    # 3.4 Mean-reversion: anti-chase / buy-dip
    if current.change_percent >= Decimal("5.00"):
        score -= Decimal("8"); risk_flags.append(f"日涨{_format_percent(current.change_percent)}，不追高")
    elif current.change_percent >= Decimal("3.00"):
        score -= Decimal("4")
    elif current.change_percent <= Decimal("-5.00"):
        score += Decimal("6"); rationale.append(f"日跌{_format_percent(current.change_percent)}，关注止跌")
    elif current.change_percent <= Decimal("-3.00"):
        score += Decimal("3")

    # 3.5 Price vs auction open
    if current.current_price >= current.open_price > 0:
        score += Decimal("3")
    elif current.open_price > 0 and current.current_price < current.open_price:
        score -= Decimal("3")

    # ═══════════════════════════════════════════════════════════
    # PHASE 4: Position Context Modulation
    # ═══════════════════════════════════════════════════════════
    if holding_return_pct is not None and has_position:
        if position_ctx == "winner":
            score += Decimal("8")
            rationale.append(f"浮盈{_format_percent(holding_return_pct)}，保护利润")
        elif position_ctx == "normal":
            score += Decimal("4")
            if holding_return_pct >= Decimal("-5") and holding_return_pct <= Decimal("2"):
                score += Decimal("6")
                rationale.append(f"浮亏仅{_format_percent(abs(holding_return_pct))}，正常波动")
        elif position_ctx == "loser":
            score -= Decimal("5")
            risk_flags.append(f"浮亏{_format_percent(holding_return_pct)}，谨慎")
        elif position_ctx == "deep_loss":
            score -= Decimal("15")
            recovery = abs(Decimal("100") * holding_return_pct / (Decimal("100") + holding_return_pct))
            risk_flags.append(f"深套{_format_percent(holding_return_pct)}，回本需涨{_format_percent(recovery)}，禁补")

    # ═══════════════════════════════════════════════════════════
    # PHASE 5: Stop Loss & Take Profit
    # ═══════════════════════════════════════════════════════════
    if has_position and portfolio_holding is not None and portfolio_holding.cost_price > 0:
        from .stop_loss import compute_effective_stop as compute_stop
        _eff_stop, _label, dist_to_stop = compute_stop(
            cost_price=portfolio_holding.cost_price,
            current_price=current.current_price,
            stop_loss_pct=monitor_config.stop_loss_pct,
        )
        if dist_to_stop <= 0:
            stop_gap_pct = (((_eff_stop - current.current_price) / _eff_stop * 100) if _eff_stop > 0 else Decimal("0"))
            if stop_gap_pct >= Decimal("15"):
                score -= Decimal("3")
                risk_flags.append(f"止损价{_eff_stop}远高于现价，反弹减仓优于割肉")
            else:
                score -= Decimal("12")
                risk_flags.append(f"已触及{_label}价{_eff_stop}，建议止损")
        elif dist_to_stop <= Decimal("2.50"):
            score -= Decimal("6")
            risk_flags.append(f"距{_label}价{_eff_stop}仅{_format_percent(dist_to_stop)}")

    # Take-profit tiers
    if has_position and holding_return_pct is not None and monitor_config.take_profit_tiers:
        for tier in monitor_config.take_profit_tiers:
            if holding_return_pct >= Decimal(str(tier.profit_pct)):
                tier_sell_ratio = Decimal(str(tier.sell_ratio))
                if tier_sell_ratio <= 0:
                    risk_flags.append(f"📢 {tier.label}：浮盈{_format_percent(holding_return_pct)}，接近止盈")
                else:
                    sell_qty = int(portfolio_holding.quantity * tier_sell_ratio)
                    sell_qty = (sell_qty // 100) * 100 if sell_qty >= 100 else max(sell_qty, 100)
                    is_full = tier_sell_ratio >= Decimal("1.0") or sell_qty >= portfolio_holding.quantity
                    score -= Decimal("20") if is_full else Decimal("12")
                    risk_flags.append(f"🎯 {tier.label}触发：卖出{sell_qty}股({'清仓' if is_full else '分批'}止盈)")
                    rationale.append(f"止盈纪律：{tier.label}触发")
                break

    # ═══════════════════════════════════════════════════════════
    # PHASE 6: Portfolio Guards
    # ═══════════════════════════════════════════════════════════
    if portfolio_cash_ratio is not None:
        if portfolio_cash_ratio < Decimal("0.15"):
            score -= Decimal("5"); risk_flags.append("现金偏低")
        elif portfolio_cash_ratio >= Decimal("0.40"):
            score += Decimal("2")

    if portfolio_position_ratio is not None:
        if portfolio_position_ratio >= Decimal("0.60"):
            score -= Decimal("8"); risk_flags.append("单票集中度过高")
        elif portfolio_position_ratio >= Decimal("0.50"):
            score -= Decimal("4")

    # ═══════════════════════════════════════════════════════════
    # PHASE 7: Limit Boards & Filters
    # ═══════════════════════════════════════════════════════════
    if current.change_percent <= Decimal("-9.5"):
        score -= Decimal("15"); risk_flags.append("跌停板：不可操作")
    elif current.change_percent >= Decimal("9.5"):
        score -= Decimal("8"); risk_flags.append("涨停板：追涨风险极高")

    # MA240 + Gap
    if metrics.ma240 > 0:
        if current.current_price >= metrics.ma240:
            score += Decimal("4")
        else:
            score -= Decimal("4")

    gap_pct = (_percent_diff(current.open_price, current.previous_close)
               if current.previous_close > 0 and current.open_price > 0 else Decimal("0"))
    if gap_pct >= Decimal("2.50"):
        score -= Decimal("5"); risk_flags.append(f"高开{_format_percent(gap_pct)}")
    elif gap_pct <= Decimal("-2.50"):
        score -= Decimal("6"); risk_flags.append(f"大幅低开{_format_percent(gap_pct)}")

    # Market regime
    regime = _market_regime(current, metrics)
    if regime == "distribution":
        score -= Decimal("6"); risk_flags.append("量价背离，警惕派发")
    elif regime == "recovery":
        score += Decimal("4"); rationale.append("超卖反弹迹象")

    if not rationale:
        rationale.append("多空均衡，等待明确信号")

    # ═══════════════════════════════════════════════════════════
    # PHASE 7.5: UZI Multi-Dimensional Enhancement (inspired by UZI-Skill)
    #           量价背离 · 相对强弱 · 波动异动 · 连续方向
    # ═══════════════════════════════════════════════════════════
    try:
        from .uzi_scoring import analyze_uzi_signals
        # Build simplified minute bars from metrics (we don't have full OHLCV per bar,
        # but we can approximate direction from step_change_pct and volume)
        recent_bars = []
        for i in range(min(5, sample_size)):
            recent_bars.append({
                "close": float(current.current_price) * (1 + 0.001 * (i - 2)),  # rough approximation
                "volume": float(metrics.avg5_minute_volume_shares),
            })

        uzi_score, uzi_summary, uzi_signals = analyze_uzi_signals(
            current_price=current.current_price,
            current_volume=metrics.minute_volume_shares,
            current_change=current.change_percent,
            current_amplitude=metrics.intraday_amplitude_pct,
            avg_volume_5=metrics.avg5_minute_volume_shares,
            avg_volume_20=metrics.avg30_minute_volume_shares,
            avg_amplitude_10=metrics.intraday_amplitude_pct,  # use current as proxy
            index_change=metrics.benchmark_change_pct,
            sector_change=None,
            recent_minute_bars=recent_bars,
        )
        if uzi_score != 0:
            score += Decimal(str(uzi_score))
            if uzi_score > 0:
                rationale.append(f"UZI增强: {uzi_summary}")
            else:
                risk_flags.append(f"UZI警告: {uzi_summary}")
    except Exception:
        pass  # UZI scoring is best-effort, never block the pipeline

    # ═══════════════════════════════════════════════════════════
    # PHASE 7.6: Wyckoff Volume-Price Structure (inspired by WyckoffTradingAgent ⭐421)
    #            Climax detection · Accumulation/Distribution · Spring patterns
    # ═══════════════════════════════════════════════════════════
    try:
        from .wyckoff_scoring import analyze_wyckoff

        # Compute derived ratios
        _wyckoff_ma20 = daily_ma20 if daily_ma20 and daily_ma20 > 0 else metrics.ma15
        _wyckoff_ma60 = daily_ma60 if daily_ma60 and daily_ma60 > 0 else metrics.ma60
        price_vs_ma20 = (current.current_price / _wyckoff_ma20) if _wyckoff_ma20 and _wyckoff_ma20 > 0 else Decimal("1")
        price_vs_ma60 = (current.current_price / _wyckoff_ma60) if _wyckoff_ma60 and _wyckoff_ma60 > 0 else Decimal("1")

        # Volume trend heuristic
        if daily_vol_ratio is not None and daily_vol_ratio < Decimal("0.7"):
            vol_trend = "shrinking"
        elif daily_vol_ratio is not None and daily_vol_ratio > Decimal("1.3"):
            vol_trend = "expanding"
        else:
            vol_trend = "stable"

        # Use daily RSI if available, fallback to minute RSI
        _wyckoff_rsi = daily_rsi14 if daily_rsi14 is not None else metrics.rsi14

        wyckoff_score, wyckoff_summary, wyckoff_signals = analyze_wyckoff(
            change_pct=current.change_percent,
            vol_ratio=metrics.volume_ratio,
            amplitude_pct=metrics.intraday_amplitude_pct,
            rsi14=_wyckoff_rsi,
            price_vs_ma20=price_vs_ma20,
            price_vs_ma60=price_vs_ma60,
            vol_trend=vol_trend,
            daily_vol_shrinking=(daily_vol_ratio is not None and daily_vol_ratio < Decimal("0.85")),
        )
        if wyckoff_score != 0:
            score += Decimal(str(wyckoff_score))
            if wyckoff_score > 0:
                rationale.append(f"威科夫: {wyckoff_summary}")
            else:
                risk_flags.append(f"威科夫: {wyckoff_summary}")
    except Exception:
        pass  # Wyckoff scoring is best-effort

    # ═══════════════════════════════════════════════════════════
    # PHASE 7.7: Sector Strength Boost — 板块强度加分
    #           持仓所在板块今日表现 → score +3~+5
    # ═══════════════════════════════════════════════════════════
    try:
        from .sector_strength import fetch_sector_boards, compute_sector_score_boost
        _sectors = fetch_sector_boards(top_n=60)
        sector_boost = compute_sector_score_boost(_sectors, current.symbol)
        if sector_boost != 0:
            score += Decimal(str(sector_boost))
            if sector_boost > 0:
                rationale.append(f"板块强度加分: +{sector_boost}")
            else:
                risk_flags.append(f"板块弱势罚分: {sector_boost}")
    except Exception:
        pass

    # ═══════════════════════════════════════════════════════════
    # PHASE 7.8: Oversold Bounce Bonus — independent buy signal
    #            RSI oversold + volume confirmation = accumulation
    #            NOT blocked by daily regime — buying opportunity
    # ═══════════════════════════════════════════════════════════
    vol_ok = metrics.volume_ratio >= Decimal("0.8")
    rsi_low = metrics.rsi14 <= Decimal("40")
    if rsi_low and vol_ok:
        if metrics.rsi14 <= Decimal("35") and metrics.volume_ratio >= Decimal("1.0"):
            score += Decimal("10")
            rationale.append("超卖+放量：底部吸筹信号，买入窗口")
        elif metrics.rsi14 <= Decimal("30") and metrics.volume_ratio >= Decimal("0.8"):
            score += Decimal("8")
            rationale.append("深度超卖+量能确认：反弹概率高")
        elif metrics.rsi14 <= Decimal("40") and metrics.volume_ratio >= Decimal("1.2"):
            score += Decimal("6")
            rationale.append("低位放量：资金入场迹象")

    # ═══════════════════════════════════════════════════════════
    # PHASE 8: Decision & Hard Guards
    # ═══════════════════════════════════════════════════════════
    score = max(Decimal("0"), min(score, Decimal("100")))
    action = _decision_action(score, monitor_config, metrics.benchmark_change_pct, metrics.market_advance_ratio)

    action, guard_rationales, guard_risk_flags = _apply_account_risk_guards(
        action, monitor_config, portfolio_holding,
        portfolio_cash_ratio=portfolio_cash_ratio,
        portfolio_position_ratio=portfolio_position_ratio,
    )
    rationale.extend(guard_rationales)
    risk_flags.extend(guard_risk_flags)

    # Market crash guard
    market_crash = (
        (metrics.benchmark_change_pct <= Decimal("-2.00") and metrics.market_advance_ratio <= Decimal("0.35"))
        or metrics.benchmark_change_pct <= Decimal("-3.50")
        or metrics.market_advance_ratio <= Decimal("0.20")
    )
    if market_crash and action in ("buy", "hold"):
        action = "avoid"
        risk_flags.append(f"大盘暴跌，覆巢之下无完卵")
        rationale.append("大盘系统性风险")

    # Anti-chase guard: relaxed from 3% to 5% — allow moderate trend buys
    if current.change_percent >= Decimal("5.00") and action in ("buy", "hold"):
        action = "avoid"
        risk_flags.append(f"反追涨护栏：日涨{_format_percent(current.change_percent)}≥5%")
        rationale.append("不追高纪律")

    # Multi-timeframe block: weekly bear → suppress buy
    if mtf_block_buy and action == "buy":
        action = "avoid"
        risk_flags.append("多周期护栏: 周线空头，禁止买入")
        rationale.append("周线空头禁买")

    # Multi-timeframe block: weekly+day bull → suppress sell
    if mtf_block_sell and action in ("sell", "reduce"):
        action = "hold"
        rationale.append("多周期护栏: 趋势强劲，暂缓卖出")

    # Ex-dividend guard
    if recent_ex_dividend and action in ("buy", "hold"):
        action = "avoid"
        risk_flags.append("除权除息预警")
        rationale.append("除权除息防护")

    # Stop-profit guard
    if any("🎯" in flag for flag in risk_flags) and action in ("buy", "hold", "avoid"):
        action = "reduce"
        rationale.append("止盈纪律强制")

    # ═══════════════════════════════════════════════════════════
    # PHASE 9: Trade Plan
    # ═══════════════════════════════════════════════════════════
    trade_advice, trade_size_hint, entry_note = _trade_plan(
        action, score, current, metrics, portfolio_holding,
        trading_habit_profile,
        monitor_config=monitor_config,
        total_assets=portfolio_total_assets,
    )

    # Log signal for accuracy tracking
    try:
        from .signal_tracker import log_signal
        log_signal(
            symbol=current.symbol,
            name=current.name or current.symbol,
            action=action,
            score=score,
            price=current.current_price,
            confidence=_confidence_level(score, sample_size),
            regime=regime,
            rationale="; ".join(rationale[:3]),
        )
    except Exception:
        pass

    return DecisionSignal(
        action=action,
        score=score.quantize(Decimal("0.01")),
        confidence=_confidence_level(score, sample_size),
        regime=regime,
        trade_advice=trade_advice,
        trade_size_hint=trade_size_hint,
        entry_note=entry_note,
        rationale=rationale,
        risk_flags=risk_flags,
    )


def _trade_plan(
    action: str,
    score: Decimal,
    current: StockQuote,
    metrics: ObservationMetrics,
    portfolio_holding: PortfolioHolding | None,
    trading_habit_profile: TradingHabitProfile | None,
    *,
    monitor_config: MonitorConfig | None = None,
    total_assets: Decimal | None = None,
) -> tuple[str, str, str]:
    habit_note = _habit_note(trading_habit_profile)
    if action == "buy":
        tier_label, buy_qty, tier_pct = _recommended_buy_quantity(
            score, portfolio_holding, trading_habit_profile,
            total_assets=total_assets, current_price=current.current_price,
            position_tiers=monitor_config.position_tiers if monitor_config else None,
        )
        # Compute entry price zone: don't chase above MA15, floor at MA60
        entry_ceiling = min(current.current_price, metrics.ma15) if metrics.ma15 > 0 else current.current_price
        entry_floor = max(metrics.ma60 * Decimal("0.98"), current.current_price * Decimal("0.97")) if metrics.ma60 > 0 else current.current_price * Decimal("0.98")
        entry_floor = min(entry_floor, entry_ceiling * Decimal("0.98"))  # always some spread
        stop_level = entry_floor * Decimal("0.93")  # -7% stop from entry floor (fallback)
        # Try ATR-based dynamic stop-loss for smarter risk management
        try:
            from .atr_risk import compute_atr_stop
            atr_result = compute_atr_stop(
                current.symbol, current.current_price, entry_floor,
            )
            if atr_result:
                stop_level = atr_result.dynamic_stop
                entry_note = (
                    f"ATR动态止损 {stop_level:.2f}"
                    f"（-{atr_result.dynamic_stop_pct:.1f}%，{atr_result.volatility_level}波动）"
                )
        except Exception:
            pass  # ATR是增强特性，失败了用固定止损
        tier_tag = f"[{tier_label}] " if tier_label else ""
        return (
            f"{tier_tag}挂单 {_format_price(entry_floor)}-{_format_price(entry_ceiling)} 买入 {buy_qty} 股{habit_note}",
            f"{tier_tag}入场区间 {_format_price(entry_floor)}-{_format_price(entry_ceiling)}，{buy_qty} 股",
            f"止损 {_format_price(stop_level)}（-7%），不追高 MA15 {_format_price(metrics.ma15)} 上方",
        )
    if action == "hold":
        hold_qty = portfolio_holding.quantity if portfolio_holding is not None and portfolio_holding.quantity > 0 else 0
        hold_hint = f"继续持有 {hold_qty} 股" if hold_qty > 0 else "保持空仓"
        return (
            f"持有 {hold_qty} 股，禁止加仓{habit_note}" if hold_qty > 0 else f"空仓观望，禁止开仓{habit_note}",
            hold_hint,
            f"守住 MA60 {_format_price(metrics.ma60)} 就不动；跌破则转减仓，反弹缩量也不追",
        )
    if action == "reduce":
        reduce_qty = _recommended_reduce_quantity(score, portfolio_holding, trading_habit_profile)
        return (
            f"卖出 {reduce_qty} 股{habit_note}",
            f"先减 {reduce_qty} 股，回收现金",
            f"优先在反弹靠近 MA60 {_format_price(metrics.ma60)} 时挂卖；若继续跌弱，按止损纪律执行",
        )
    avoid_qty = _recommended_avoid_quantity(portfolio_holding, trading_habit_profile)
    has_position = portfolio_holding is not None and portfolio_holding.quantity > 0
    return (
        f"禁止买入；卖出 {avoid_qty} 股{habit_note}" if has_position else f"禁止买入，空仓等待{habit_note}",
        f"先减仓 {avoid_qty} 股，停止加仓" if has_position else "空仓等待下一次机会",
        f"等价格重新站回 MA15 {_format_price(metrics.ma15)} 且量能恢复后再评估，不在弱势里硬接",
    )


def _recommended_buy_quantity(
    score: Decimal,
    portfolio_holding: PortfolioHolding | None,
    trading_habit_profile: TradingHabitProfile | None,
    *,
    total_assets: Decimal | None = None,
    current_price: Decimal | None = None,
    position_tiers: list | None = None,
) -> tuple[str, int, Decimal]:
    """
    Return (tier_label, quantity, tier_pct).

    Position sizing uses 3 tiers from config (default: 试探仓/标准仓/确信仓).
    Risk adjustments:
      - Adding to winning position (pnl >= 5%): bump up one tier
      - Deep loss position (pnl <= -20%): force lowest tier
      - New position (no holding): cap at standard tier (no conviction for untested)
      - Market crash context: caller must pass reduced action before calling here
    """
    # Default tiers if none provided (fallback for tests/cli without config)
    if not position_tiers:
        from stock_advisor.config import PositionTierConfig
        position_tiers = [
            PositionTierConfig(score_min=78.0, pct_of_assets=0.03, label="试探仓"),
            PositionTierConfig(score_min=84.0, pct_of_assets=0.05, label="标准仓"),
            PositionTierConfig(score_min=90.0, pct_of_assets=0.08, label="确信仓"),
        ]

    # Determine base tier by score
    selected_tier = position_tiers[0]  # lowest tier (试探仓)
    for tier in position_tiers:
        if float(score) >= tier.score_min:
            selected_tier = tier

    # Risk adjustments
    has_position = portfolio_holding is not None and portfolio_holding.quantity > 0
    is_new_position = not has_position

    if has_position and portfolio_holding is not None:
        pnl_pct = _safe_pnl_pct(portfolio_holding)
        # Deep loss: force probe tier
        if pnl_pct is not None and pnl_pct <= Decimal("-20"):
            selected_tier = position_tiers[0]  # 试探仓 only
        # Winning position: bump up one tier (add more confidence)
        elif pnl_pct is not None and pnl_pct >= Decimal("5"):
            current_idx = position_tiers.index(selected_tier)
            if current_idx < len(position_tiers) - 1:
                selected_tier = position_tiers[current_idx + 1]

    # New position: cap at standard tier
    if is_new_position:
        standard_idx = min(1, len(position_tiers) - 1)  # index 1 = standard
        if position_tiers.index(selected_tier) > standard_idx:
            selected_tier = position_tiers[standard_idx]

    # Trading habit override (if enough history)
    if trading_habit_profile is not None and trading_habit_profile.sample_count >= 3:
        if has_position:
            habit_qty = trading_habit_profile.preferred_add_lot
        else:
            habit_qty = trading_habit_profile.preferred_buy_lot
        if habit_qty > 0:
            return (selected_tier.label, habit_qty, Decimal(str(selected_tier.pct_of_assets)))

    # Compute quantity from tier pct * total_assets
    if total_assets is not None and total_assets > 0 and current_price is not None and current_price > 0:
        tier_pct = Decimal(str(selected_tier.pct_of_assets))
        target_value = total_assets * tier_pct
        qty = int(target_value / current_price)
        qty = (qty // 100) * 100
        return (selected_tier.label, max(100, qty), tier_pct)

    # Absolute fallback (no total_assets available)
    if score >= Decimal("90"):
        return (selected_tier.label, 300, Decimal(str(selected_tier.pct_of_assets)))
    if score >= Decimal("84"):
        return (selected_tier.label, 200, Decimal(str(selected_tier.pct_of_assets)))
    return (selected_tier.label, 100, Decimal(str(selected_tier.pct_of_assets)))


def _safe_pnl_pct(holding: PortfolioHolding) -> Decimal | None:
    """Compute pnl_pct safely, returning None if missing fields or zero cost."""
    try:
        cost = getattr(holding, "cost_price", None)
        current = getattr(holding, "current_price", None)
    except Exception:
        return None
    if cost is None or current is None or cost == Decimal("0"):
        return None
    return (current - cost) / cost * Decimal("100")


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


def _ma_alignment_score(price: Decimal, ma5: Decimal, ma15: Decimal, ma60: Decimal, ma240: Decimal) -> int:
    """Returns MA stack alignment score.
    +4 = price>MA5>MA15>MA60>MA240 (full bull stack)
    -4 = price<MA5<MA15<MA60<MA240 (full bear stack)
    Intermediate values reflect partial alignment."""
    levels = [v for v in [price, ma5, ma15, ma60, ma240] if v > 0]
    if len(levels) < 2:
        return 0
    total = len(levels) - 1
    bull = sum(1 for a, b in zip(levels, levels[1:]) if a > b)
    bear = sum(1 for a, b in zip(levels, levels[1:]) if a < b)
    if bull == total:
        return total
    if bear == total:
        return -total
    return bull - bear


def _compute_threshold_shift(benchmark_change_pct: Decimal, market_advance_ratio: Decimal) -> Decimal:
    """Dynamic threshold shift combining benchmark trend and market breadth.
    Negative = lower bar (bullish market), Positive = raise bar (bearish market).
    Capped at ±6 to prevent threshold collapse."""
    if benchmark_change_pct >= Decimal("1.5"):
        bench = Decimal("-5")
    elif benchmark_change_pct >= Decimal("1.0"):
        bench = Decimal("-3")
    elif benchmark_change_pct <= Decimal("-1.5"):
        bench = Decimal("5")
    elif benchmark_change_pct <= Decimal("-1.0"):
        bench = Decimal("3")
    else:
        bench = Decimal("0")
    if market_advance_ratio >= Decimal("0.70"):
        breadth = Decimal("-3")
    elif market_advance_ratio >= Decimal("0.60"):
        breadth = Decimal("-1")
    elif Decimal("0") < market_advance_ratio <= Decimal("0.30"):
        breadth = Decimal("3")
    elif Decimal("0") < market_advance_ratio <= Decimal("0.40"):
        breadth = Decimal("1")
    else:
        breadth = Decimal("0")
    return max(Decimal("-6"), min(bench + breadth, Decimal("6")))


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


def _describe_ma_alignment(align: int) -> str:
    labels = {4: "完整多头排列 ▲▲▲▲", 3: "多头排列为主 ▲▲▲", 2: "偏多排列 ▲▲", 1: "轻微偏多 ▲",
              0: "多空交叉 ─", -1: "轻微偏空 ▼", -2: "偏空排列 ▼▼", -3: "空头排列为主 ▼▼▼", -4: "完整空头排列 ▼▼▼▼"}
    return labels.get(align, "多空交叉 ─")


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


def _average_decimal_list(values: list[Decimal], n: int) -> Decimal | None:
    window = values[-n:] if len(values) >= n else values
    if not window:
        return None
    return (sum(window, Decimal("0")) / Decimal(len(window))).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _daily_rsi(closes: list[Decimal], period: int = 14) -> Decimal | None:
    """Compute daily RSI from closing prices."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    window = deltas[-period:]
    gains = sum((d for d in window if d > 0), Decimal("0"))
    losses = sum((-d for d in window if d < 0), Decimal("0"))
    avg_gain = gains / Decimal(period)
    avg_loss = losses / Decimal(period)
    if avg_loss == 0:
        return Decimal("100.00") if avg_gain > 0 else Decimal("50.00")
    rs = avg_gain / avg_loss
    rsi = Decimal("100") - (Decimal("100") / (Decimal("1") + rs))
    return rsi.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _daily_volume_ratio(volumes: list[Decimal], lookback: int = 5) -> Decimal | None:
    """Compute today's volume vs N-day average volume ratio.

    IMPORTANT: During intraday trading, today's volume from the daily K-line
    API is only a partial-day figure.  Comparing it directly against full-day
    averages artificially inflates "缩量" signals in the morning, triggering
    false sell decisions.  We estimate the full-day volume by scaling
    proportionally to the fraction of the trading day elapsed (capped at 3x
    to avoid over-correcting the high-volume opening minutes).
    """
    from datetime import datetime
    from .market_hours import is_a_share_trading_time

    if len(volumes) < lookback + 1:
        return None
    today_vol = volumes[-1]
    avg_vol = sum(volumes[-lookback-1:-1], Decimal("0")) / Decimal(lookback)
    if avg_vol <= 0:
        return None

    # Intraday volume scaling
    now = datetime.now()
    if is_a_share_trading_time(now):
        minutes_elapsed = (now - now.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() / 60
        if now.hour >= 13:
            minutes_elapsed -= 90
        if 10 < minutes_elapsed < 240:
            scale = min(Decimal("3.00"), Decimal(240) / Decimal(str(round(minutes_elapsed, 1))))
            today_vol = today_vol * scale

    return (today_vol / avg_vol).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _detect_ex_dividend_gap(closes: list[Decimal]) -> bool:
    """Detect recent ex-dividend gap in daily closes.

    Corporate actions (除权除息) cause a one-day price adjustment that looks like
    a crash but isn't.  Signature: a single day with >3% drop followed by a
    recovery day (not continued selling).  Detecting this prevents false buy
    signals on the recovery bounce.
    """
    if len(closes) < 5:
        return False
    # Look at the last 5 days for a single large drop followed by recovery
    changes = [(closes[i] - closes[i-1]) / closes[i-1] * 100
               for i in range(1, len(closes))]
    # Check most recent 3 changes for the pattern: big drop then bounce
    recent = changes[-4:]
    for i in range(len(recent) - 1):
        if recent[i] <= -3 and recent[i+1] >= 0:
            return True  # Drop followed by recovery → likely ex-dividend
    return False


def _format_price(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def _format_percent(value: Decimal) -> str:
    scaled = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    prefix = "+" if scaled > 0 else ""
    return f"{prefix}{scaled}%"


def _format_ratio(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _holding_return_percent(current: StockQuote, portfolio_holding: PortfolioHolding | None) -> Decimal | None:
    if portfolio_holding is None or portfolio_holding.quantity <= 0 or portfolio_holding.cost_price <= 0:
        return None
    return _percent_diff(current.current_price, portfolio_holding.cost_price)


def _format_volume(value: Decimal) -> str:
    return str(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
