from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


@dataclass(slots=True)
class HoldingState:
    code: str
    name: str
    quantity: int
    cost_price: Decimal
    current_price: Decimal
    pnl_pct: Decimal
    market_value: Decimal


@dataclass(slots=True)
class InstructionState:
    code: str
    name: str
    action: str
    quantity: int
    trigger_low: Decimal | None
    trigger_high: Decimal | None
    fallback_price: Decimal | None
    reason: str
    source: str = ""
    created_at: str = ""
    priority: int = 0


@dataclass(slots=True)
class TradingState:
    trade_date: date
    generated_at: datetime
    total_assets: Decimal
    cash: Decimal
    holdings: list[HoldingState] = field(default_factory=list)
    active_instructions: list[InstructionState] = field(default_factory=list)
    orphan_instructions: list[InstructionState] = field(default_factory=list)
    briefing_date: str = ""
    briefing_generated_at: str = ""
    briefing_summary: str = ""
