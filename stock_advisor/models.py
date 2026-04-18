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
    avg3: Decimal
    avg6: Decimal
    bias_to_avg3: Decimal
    bias_to_avg6: Decimal
    step_change_pct: Decimal
    recent_range_pct: Decimal
    intraday_amplitude_pct: Decimal


@dataclass(slots=True)
class ObservationResult:
    title: str
    message: str
    observations: List[str]
    should_notify: bool
    signal_level: str
    metrics: ObservationMetrics


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
