"""Scoring utility functions — standalone helpers.
No imports from other stock_advisor modules. Only stdlib + Decimal.
"""
from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import ObservationMetrics, StockQuote

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
