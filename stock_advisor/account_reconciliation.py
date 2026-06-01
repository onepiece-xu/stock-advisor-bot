"""Phase 5: Multi-account holding reconciliation.

Merges holdings from multiple brokerage accounts (e.g. 东吴 + 兴业)
into a single unified snapshot with weighted-average cost prices.

Key rules:
  1. Same stock across accounts → quantities sum, cost prices weight-average
  2. If one account sold but another still holds → total reflects real remaining shares
  3. Never let a single-account screenshot overwrite the combined position
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


@dataclass(slots=True)
class AccountHolding:
    """Single-account holding record."""
    account: str       # e.g. "东吴", "兴业"
    name: str
    code: str
    quantity: int
    cost_price: Decimal
    current_price: Decimal
    market_value: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.market_value == 0:
            self.market_value = (self.current_price * self.quantity).quantize(Decimal("0.01"))


@dataclass(slots=True)
class AccountSnapshot:
    """A single brokerage account's complete snapshot."""
    account: str
    trade_date: str          # ISO date
    total_assets: Decimal
    cash: Decimal
    holdings: list[AccountHolding] = field(default_factory=list)


@dataclass(slots=True)
class MergedHolding:
    """Result of merging the same stock across accounts."""
    name: str
    code: str
    quantity: int
    cost_price: Decimal      # weighted average
    current_price: Decimal   # latest (or weighted if different)
    market_value: Decimal
    pnl_pct: Decimal

    # Per-account breakdown for audit
    breakdown: list[dict[str, Any]] = field(default_factory=list)


def merge_broker_holdings(
    *accounts: AccountSnapshot,
) -> dict[str, Any]:
    """Merge holdings from multiple brokerage accounts into a single snapshot.

    Returns a dict compatible with the portfolio-snapshot.json format:
      {tradeDate, totalAssets, cash, holdings: [{name, code, quantity, costPrice, currentPrice}]}

    Args:
        accounts: One or more AccountSnapshot objects (e.g. 东吴 + 兴业).

    Raises:
        ValueError: if no accounts provided or all accounts have no holdings.
    """
    if not accounts:
        raise ValueError("At least one account snapshot is required")

    # ── Pick the latest trade date ──
    trade_date = max(acc.trade_date for acc in accounts)

    # ── Sum totals ──
    total_assets = sum((acc.total_assets for acc in accounts), Decimal("0"))
    total_cash = sum((acc.cash for acc in accounts), Decimal("0"))

    # ── Merge holdings by code ──
    by_code: dict[str, list[AccountHolding]] = {}
    for acc in accounts:
        for h in acc.holdings:
            if h.quantity <= 0:
                continue
            by_code.setdefault(h.code, []).append(h)

    merged: list[dict[str, Any]] = []
    for code, items in by_code.items():
        if not items:
            continue

        name = items[0].name  # same stock, same name
        total_qty = sum(h.quantity for h in items)

        # Weighted-average cost price
        total_cost_basis = sum(
            h.cost_price * h.quantity for h in items
        )
        avg_cost = (total_cost_basis / total_qty).quantize(Decimal("0.0001"))

        # Current price: use the latest available
        current_price = max(items, key=lambda h: h.current_price).current_price

        breakdown = [
            {
                "account": h.account,
                "quantity": h.quantity,
                "cost_price": float(h.cost_price),
                "market_value": float(h.market_value),
            }
            for h in items
        ]

        merged.append({
            "name": name,
            "code": code,
            "quantity": total_qty,
            "costPrice": float(avg_cost),
            "currentPrice": float(current_price),
            "_breakdown": breakdown,   # audit trail, stripped on serialization
        })

    if not merged:
        raise ValueError("No holdings found in any account")

    return {
        "tradeDate": trade_date,
        "totalAssets": float(total_assets),
        "cash": float(total_cash),
        "holdings": merged,
    }


def validate_merged_snapshot(
    snapshot: dict[str, Any],
    *,
    expected_total_assets: Decimal | None = None,
    expected_cash: Decimal | None = None,
) -> list[str]:
    """Validate a merged snapshot for consistency. Returns list of warnings."""
    warnings: list[str] = []

    holdings = snapshot.get("holdings", [])
    if not holdings:
        warnings.append("Snapshot has no holdings")
        return warnings

    # Check each holding has required fields
    for h in holdings:
        if not h.get("code"):
            warnings.append(f"Holding missing code: {h.get('name', '?')}")
        if h.get("quantity", 0) <= 0:
            warnings.append(f"{h.get('name', '?')}({h.get('code', '?')}) has zero quantity")
        if h.get("costPrice", 0) <= 0:
            warnings.append(f"{h.get('name', '?')}({h.get('code', '?')}) has non-positive cost")

    # Check total assets sanity
    total_market_value = sum(
        h.get("currentPrice", 0) * h.get("quantity", 0)
        for h in holdings
    )
    declared_assets = snapshot.get("totalAssets", 0)
    declared_cash = snapshot.get("cash", 0)
    implied_assets = declared_cash + total_market_value

    if abs(declared_assets - implied_assets) > 1.0:  # allow 1 yuan rounding
        warnings.append(
            f"Asset mismatch: declared {declared_assets:.0f} vs "
            f"implied {implied_assets:.0f} (cash={declared_cash:.0f} + holdings={total_market_value:.0f})"
        )

    if expected_total_assets and abs(Decimal(str(declared_assets)) - expected_total_assets) > Decimal("10"):
        warnings.append(
            f"Total assets {declared_assets:.0f} differs from expected {expected_total_assets:.0f}"
        )

    if expected_cash and abs(Decimal(str(declared_cash)) - expected_cash) > Decimal("1"):
        warnings.append(
            f"Cash {declared_cash:.0f} differs from expected {expected_cash:.0f}"
        )

    return warnings
