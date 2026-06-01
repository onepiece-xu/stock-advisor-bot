"""Shared stop-loss and take-profit computation — used by both analysis engine and runtime notifications."""

from decimal import Decimal


def compute_effective_stop(
    cost_price: Decimal,
    current_price: Decimal,
    peak_price: Decimal | None = None,
    stop_loss_pct: float = 7.0,
) -> tuple[Decimal, str, str]:
    """Compute the three-tier effective stop-loss price.

    Returns (effective_stop, tier_label, distance_pct_to_stop).

    Tiers:
    - Trailing (peak >=10%): peak * 0.97, "尾随止损"
    - Breakeven (peak >=5%): cost_price, "保本止损"
    - Fixed: cost_price * (1 - stop_pct/100), "固定止损"
    """
    stop_ratio = Decimal(str(stop_loss_pct)) / Decimal("100")
    fixed_stop = (cost_price * (Decimal("1") - stop_ratio)).quantize(Decimal("0.001"))

    if peak_price is None:
        peak_price = current_price

    float_pct = ((peak_price - cost_price) / cost_price * Decimal("100")).quantize(Decimal("0.01"))

    if float_pct >= Decimal("10"):
        trailing = (peak_price * Decimal("0.97")).quantize(Decimal("0.001"))
        effective_stop = max(fixed_stop, trailing)
        tier_label = f"尾随止损（峰值 {peak_price}，回撤 3%）"
    elif float_pct >= Decimal("5"):
        effective_stop = max(fixed_stop, cost_price)
        tier_label = "保本止损（浮盈已超 5%，止损线移至成本）"
    else:
        effective_stop = fixed_stop
        tier_label = f"固定止损 -{stop_loss_pct}%"

    if effective_stop > 0:
        distance_pct = ((current_price - effective_stop) / effective_stop * Decimal("100")).quantize(Decimal("0.01"))
    else:
        distance_pct = Decimal("0")

    return effective_stop, tier_label, distance_pct


def compute_take_profit(
    cost_price: Decimal,
    current_price: Decimal,
    peak_price: Decimal | None = None,
    *,
    trailing_drawdowns: list[tuple[float, float]] | None = None,
) -> tuple[Decimal | None, str, Decimal]:
    """Compute trailing take-profit trigger price.

    Returns (trigger_price_or_None, label, distance_pct_from_trigger).

    ONLY triggers when price has pulled back from peak by the drawdown %.
    Does NOT trigger just because profit reached a certain % — that's the
    key difference from fixed take-profit tiers (the "一刀切" problem).

    Default drawdown tiers (profit_level_pct, drawdown_pct):
      - Profit < 10%: 5% drawdown from peak
      - Profit 10-20%: 8% drawdown from peak
      - Profit >= 20%: 10% drawdown from peak

    Returns (None, label, distance) when:
      - No profit yet (peak <= cost) → "未盈利，无移动止盈"
      - Not yet pulled back enough → "移动止盈未触发，距触发线 X%"

    Returns (trigger_price, label, distance) when:
      - Price has pulled back below trailing stop → trigger signal
    """
    if trailing_drawdowns is None:
        trailing_drawdowns = [
            (10.0, 5.0),   # profit < 10%: 5% drawdown
            (20.0, 8.0),   # profit 10-20%: 8% drawdown
            (float("inf"), 10.0),  # profit >= 20%: 10% drawdown
        ]

    if peak_price is None:
        peak_price = current_price

    if peak_price <= cost_price or cost_price <= 0:
        return None, "未盈利，无移动止盈", Decimal("0")

    profit_from_cost = ((peak_price - cost_price) / cost_price * Decimal("100")).quantize(Decimal("0.01"))

    # Find the right drawdown tier
    drawdown_pct = 5.0  # default
    for profit_threshold, dd in trailing_drawdowns:
        if float(profit_from_cost) < profit_threshold:
            drawdown_pct = dd
            break

    drawdown_ratio = Decimal(str(drawdown_pct)) / Decimal("100")
    trailing_trigger = (peak_price * (Decimal("1") - drawdown_ratio)).quantize(Decimal("0.001"))

    # Distance from current price to trigger (positive = above trigger, negative = below)
    if trailing_trigger > 0:
        distance_from_trigger = ((current_price - trailing_trigger) / trailing_trigger * Decimal("100")).quantize(Decimal("0.01"))
    else:
        distance_from_trigger = Decimal("0")

    tier_desc = f"移动止盈（峰值{peak_price}，浮盈{profit_from_cost}%，回撤{drawdown_pct}%触发）"

    if distance_from_trigger <= 0:
        # Triggered — price has pulled back below trailing stop
        return trailing_trigger, f"🎯 {tier_desc}，已触发！", distance_from_trigger

    # Not triggered yet
    return None, f"📈 {tier_desc}，距触发线{_fmt(distance_from_trigger)}%", distance_from_trigger


def _fmt(d: Decimal) -> str:
    return str(d.quantize(Decimal("0.01")))
