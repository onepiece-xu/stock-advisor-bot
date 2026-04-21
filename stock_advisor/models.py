from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import List


@dataclass(slots=True)
class StockRef:
    exchange: str
    code: str

    @property
    def symbol(self) -> str:
        return f"{self.exchange}{self.code}"


@dataclass(slots=True)
class StockQuote:
    provider: str
    symbol: str
    code: str
    name: str
    current_price: Decimal
    open_price: Decimal
    previous_close: Decimal
    high_price: Decimal
    low_price: Decimal
    change_amount: Decimal
    change_percent: Decimal
    volume_shares: Decimal
    turnover_yuan: Decimal
    quote_time: datetime
    raw_payload: str

    @property
    def intraday_amplitude_percent(self) -> Decimal:
        if self.previous_close <= 0:
            return Decimal("0")
        return ((self.high_price - self.low_price) / self.previous_close * Decimal("100")).quantize(Decimal("0.01"))


@dataclass(slots=True)
class ObservationMetrics:
    ma5: Decimal
    ma15: Decimal
    ma60: Decimal
    ma240: Decimal
    rsi14: Decimal
    bias_to_ma15: Decimal
    bias_to_ma60: Decimal
    step_change_pct: Decimal
    recent_range_pct: Decimal
    intraday_amplitude_pct: Decimal
    minute_volume_shares: Decimal
    avg5_minute_volume_shares: Decimal
    avg30_minute_volume_shares: Decimal
    volume_ratio: Decimal
    volume_ratio_30: Decimal
    volume_trend_ratio: Decimal
    breakout_above_prev30_high_pct: Decimal
    breakdown_below_prev30_low_pct: Decimal
    benchmark_change_pct: Decimal
    relative_strength_pct: Decimal
    macd_line: Decimal
    macd_signal: Decimal
    macd_histogram: Decimal
    macd_prev_histogram: Decimal
    market_advance_ratio: Decimal
    hot_stock_rank: int


@dataclass(slots=True)
class DecisionSignal:
    action: str
    score: Decimal
    confidence: str
    regime: str
    rationale: List[str]
    risk_flags: List[str]
    trade_advice: str
    trade_size_hint: str
    entry_note: str


@dataclass(slots=True)
class ObservationResult:
    title: str
    message: str
    observations: List[str]
    should_notify: bool
    signal_level: str
    metrics: ObservationMetrics
    decision: DecisionSignal


@dataclass(slots=True)
class ActionCandidate:
    action: str
    reason: str
    trigger: str
    risk_level: str


@dataclass(slots=True)
class PortfolioHolding:
    name: str
    code: str
    quantity: int
    cost_price: Decimal = Decimal("0")
    current_price: Decimal = Decimal("0")


@dataclass(slots=True)
class PortfolioSnapshot:
    trade_date: date
    total_assets: Decimal = Decimal("0")
    cash: Decimal = Decimal("0")
    holdings: List[PortfolioHolding] = field(default_factory=list)


@dataclass(slots=True)
class TradeFillRecord:
    side: str
    code: str
    quantity: int
    price: Decimal
    before_quantity: int
    after_quantity: int
    filled_at: datetime


@dataclass(slots=True)
class TradingHabitProfile:
    sample_count: int
    buy_count: int
    sell_count: int
    preferred_buy_lot: int
    preferred_add_lot: int
    preferred_reduce_ratio: Decimal
    buy_style: str
    sell_style: str
    summary: str
