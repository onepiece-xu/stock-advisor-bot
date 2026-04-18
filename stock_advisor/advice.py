from __future__ import annotations

from decimal import Decimal

from .models import ActionCandidate, StockQuote


STRONG_UP_DAY_PCT = Decimal("2.00")
WEAK_DOWN_DAY_PCT = Decimal("-2.00")
HIGH_AMPLITUDE_PCT = Decimal("4.00")
KEY_PRICE_THRESHOLD = Decimal("32.00")


def build_action_candidates(current: StockQuote) -> list[ActionCandidate]:
    candidates: list[ActionCandidate] = []
    change_percent = current.change_percent.quantize(Decimal("0.01"))
    intraday_amplitude = current.intraday_amplitude_percent.quantize(Decimal("0.01"))

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

    if current.current_price >= KEY_PRICE_THRESHOLD:
        candidates.append(
            ActionCandidate(
                action="hold",
                reason="价格仍在关键位上方，可继续观察能否站稳并延续修复。",
                trigger="现价 >= 关键价位",
                risk_level="low",
            )
        )
    else:
        candidates.append(
            ActionCandidate(
                action="avoid",
                reason="价格仍未站稳关键位，当前更适合等待确认而不是主动补仓。",
                trigger="现价 < 关键价位",
                risk_level="medium",
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
        f"动作候选：{candidate.action}，原因：{candidate.reason}，触发条件：{candidate.trigger}，风险：{candidate.risk_level}。仅供参考，不构成投资建议。"
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
