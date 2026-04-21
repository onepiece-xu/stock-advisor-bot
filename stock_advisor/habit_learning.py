from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from statistics import median

from .models import TradeFillRecord, TradingHabitProfile
from .storage import load_trade_fills


def build_trading_habit_profile(conn, *, min_samples: int = 3) -> TradingHabitProfile | None:
    fills = load_trade_fills(conn)
    if len(fills) < min_samples:
        return None

    buy_fills = [fill for fill in fills if fill.side == "buy"]
    sell_fills = [fill for fill in fills if fill.side == "sell"]
    buy_quantities = [fill.quantity for fill in buy_fills]
    sell_quantities = [fill.quantity for fill in sell_fills]
    add_quantities = [fill.quantity for fill in buy_fills if fill.before_quantity > 0]
    sell_ratios = [
        (Decimal(fill.quantity) / Decimal(fill.before_quantity)).quantize(Decimal("0.0001"))
        for fill in sell_fills
        if fill.before_quantity > 0
    ]

    preferred_buy_lot = _round_to_lot(_median_int(buy_quantities, default=100))
    preferred_add_lot = _round_to_lot(_median_int(add_quantities, default=preferred_buy_lot or 100))
    preferred_reduce_ratio = _median_decimal(sell_ratios, default=Decimal("0.30"))
    buy_style = _buy_style(preferred_buy_lot)
    sell_style = _sell_style(preferred_reduce_ratio)
    summary = _build_summary(
        preferred_buy_lot=preferred_buy_lot,
        preferred_add_lot=preferred_add_lot,
        preferred_reduce_ratio=preferred_reduce_ratio,
        buy_style=buy_style,
        sell_style=sell_style,
    )

    return TradingHabitProfile(
        sample_count=len(fills),
        buy_count=len(buy_fills),
        sell_count=len(sell_fills),
        preferred_buy_lot=preferred_buy_lot,
        preferred_add_lot=preferred_add_lot,
        preferred_reduce_ratio=preferred_reduce_ratio,
        buy_style=buy_style,
        sell_style=sell_style,
        summary=summary,
    )


def render_trading_habit_profile(profile: TradingHabitProfile | None, *, mobile: bool = False) -> str:
    lines = ["【交易习惯画像】"]
    if profile is None:
        lines.append("样本不足，暂未形成稳定画像。先继续回传成交记录。")
        return "\n".join(lines)

    lines.extend(
        [
            f"样本数: {profile.sample_count} | 买入 {profile.buy_count} | 卖出 {profile.sell_count}",
            f"常用开仓: {profile.preferred_buy_lot} 股",
            f"常用加仓: {profile.preferred_add_lot} 股",
            f"常用减仓比例: {_fmt_pct(profile.preferred_reduce_ratio * Decimal('100'))}",
            f"买入风格: {profile.buy_style}",
            f"卖出风格: {profile.sell_style}",
            f"结论: {profile.summary}",
        ]
    )
    if not mobile:
        lines.append("说明: 画像会随新的真实成交记录自动更新。")
    return "\n".join(lines)


def _median_int(values: list[int], *, default: int) -> int:
    if not values:
        return default
    return int(round(median(values)))


def _median_decimal(values: list[Decimal], *, default: Decimal) -> Decimal:
    if not values:
        return default
    return Decimal(str(median(values))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _round_to_lot(quantity: int) -> int:
    if quantity <= 0:
        return 100
    if quantity < 100:
        return 100
    rounded = int((Decimal(quantity) / Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) * 100
    return max(100, rounded)


def _buy_style(preferred_buy_lot: int) -> str:
    if preferred_buy_lot <= 100:
        return "谨慎试单型"
    if preferred_buy_lot <= 300:
        return "分批加仓型"
    return "进攻放大型"


def _sell_style(preferred_reduce_ratio: Decimal) -> str:
    if preferred_reduce_ratio <= Decimal("0.25"):
        return "轻减仓分批型"
    if preferred_reduce_ratio <= Decimal("0.45"):
        return "中等分批型"
    return "果断减仓型"


def _build_summary(
    *,
    preferred_buy_lot: int,
    preferred_add_lot: int,
    preferred_reduce_ratio: Decimal,
    buy_style: str,
    sell_style: str,
) -> str:
    return (
        f"你更像 {buy_style} + {sell_style}，"
        f"常用买入 {preferred_buy_lot} 股，加仓 {preferred_add_lot} 股，"
        f"减仓习惯约 {_fmt_pct(preferred_reduce_ratio * Decimal('100'))}"
    )


def _fmt_pct(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"
