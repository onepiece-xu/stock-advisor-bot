"""Shared stop-loss computation — used by both analysis engine and runtime notifications."""

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
