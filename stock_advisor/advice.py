from __future__ import annotations

from decimal import Decimal

from .models import ActionCandidate, StockQuote


STRONG_UP_DAY_PCT = Decimal("2.00")
WEAK_DOWN_DAY_PCT = Decimal("-2.00")
HIGH_AMPLITUDE_PCT = Decimal("4.00")


def build_action_candidates(current: StockQuote) -> list[ActionCandidate]:
    candidates: list[ActionCandidate] = []
    change_percent = current.change_percent.quantize(Decimal("0.01"))
    intraday_amplitude = current.intraday_amplitude_percent.quantize(Decimal("0.01"))
    close_location = _close_location(current)

    if change_percent >= STRONG_UP_DAY_PCT:
        candidates.append(
            ActionCandidate(
                action="reduce",
                reason="当日涨幅较大，更适合借反弹优化仓位，不建议追着加仓。",
                trigger="日涨跌幅 >= 2%",
                risk_level="medium",
            )
        )
    elif change_percent <= WEAK_DOWN_DAY_PCT:
        candidates.append(
            ActionCandidate(
                action="avoid",
                reason="当日回撤偏大，优先等待企稳，不适合逆势补仓。",
                trigger="日涨跌幅 <= -2%",
                risk_level="high",
            )
        )

    if intraday_amplitude >= HIGH_AMPLITUDE_PCT:
        candidates.append(
            ActionCandidate(
                action="reduce",
                reason="盘中振幅偏大，若要处理更适合分批而不是一次性做满。",
                trigger="振幅 >= 4%",
                risk_level="medium",
            )
        )

    if close_location >= Decimal("0.75"):
        candidates.append(
            ActionCandidate(
                action="hold",
                reason="价格接近日内高位收敛，短线承接相对更稳，适合先观察延续性。",
                trigger="现价位于日内波动区间上沿",
                risk_level="low",
            )
        )
    elif close_location <= Decimal("0.25"):
        candidates.append(
            ActionCandidate(
                action="avoid",
                reason="价格靠近日内低位，短线承接仍偏弱，更适合等待止跌确认。",
                trigger="现价位于日内波动区间下沿",
                risk_level="medium",
            )
        )
    elif current.current_price >= current.previous_close:
        candidates.append(
            ActionCandidate(
                action="hold",
                reason="价格仍守在昨收附近之上，暂时没有明显破位信号。",
                trigger="现价 >= 昨收",
                risk_level="low",
            )
        )

    if not candidates:
        candidates.append(
            ActionCandidate(
                action="hold",
                reason="当前未触发明显动作条件，先观察，不急着补仓。",
                trigger="无强信号",
                risk_level="low",
            )
        )
    return _dedupe_candidates(candidates)


def render_action_candidates(candidates: list[ActionCandidate]) -> list[str]:
    return [
        f"参考动作：{candidate.action}，原因：{candidate.reason}，触发条件：{candidate.trigger}，风险：{candidate.risk_level}。仅供参考，不构成投资建议。"
        for candidate in candidates
    ]


def _dedupe_candidates(candidates: list[ActionCandidate]) -> list[ActionCandidate]:
    seen: set[tuple[str, str]] = set()
    deduped: list[ActionCandidate] = []
    for candidate in candidates:
        key = (candidate.action, candidate.reason)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _close_location(current: StockQuote) -> Decimal:
    intraday_range = current.high_price - current.low_price
    if intraday_range <= 0:
        return Decimal("0.50")
    return ((current.current_price - current.low_price) / intraday_range).quantize(Decimal("0.01"))
