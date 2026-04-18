from __future__ import annotations

from decimal import Decimal

from .models import StockQuote


STRONG_UP_DAY_PCT = Decimal("2.00")
WEAK_DOWN_DAY_PCT = Decimal("-2.00")
HIGH_AMPLITUDE_PCT = Decimal("4.00")


def build_position_advice(current: StockQuote) -> list[str]:
    advices: list[str] = []
    change_percent = current.change_percent.quantize(Decimal("0.01"))
    intraday_amplitude = current.intraday_amplitude_percent.quantize(Decimal("0.01"))
    symbol = current.symbol

    if symbol == "sz003035":
        _build_nan_wang_energy_advice(advices, change_percent, intraday_amplitude)
    elif symbol == "sh601698":
        _build_china_satcom_advice(advices, change_percent, intraday_amplitude, current.current_price)
    elif symbol == "sh603993":
        _build_cmoc_advice(advices, change_percent, intraday_amplitude, current.current_price)
    else:
        _build_generic_advice(advices, change_percent, intraday_amplitude)

    if not advices:
        advices.append("建议：当前先以观望为主，没有明显确认前，不急着补仓，优先保留仓位调整空间。仅供参考，不构成投资建议。")
    return advices


def _build_nan_wang_energy_advice(advices: list[str], change_percent: Decimal, intraday_amplitude: Decimal) -> None:
    advices.append("建议：南网能源优先承担释放现金的角色；如果你整体仓位仍重，更适合先减一笔，而不是继续往里补。仅供参考，不构成投资建议。")
    if change_percent >= STRONG_UP_DAY_PCT:
        advices.append("建议：南网能源当日走强时，可考虑借反弹分批减仓，先把部分浮盈或仓位主动权拿回来。仅供参考，不构成投资建议。")
    elif change_percent <= WEAK_DOWN_DAY_PCT:
        advices.append("建议：南网能源当日转弱时，不建议把它当成补仓对象；更合理的是等止跌后再评估，今天先别情绪化加仓。仅供参考，不构成投资建议。")
    else:
        advices.append("建议：南网能源当前更适合作为机动仓处理，若没有明显继续走强，优先考虑小幅减仓换回现金缓冲。仅供参考，不构成投资建议。")
    if intraday_amplitude >= HIGH_AMPLITUDE_PCT:
        advices.append("建议：南网能源盘中波动偏大，操作上尽量分批卖，不要一把全砍，也别在剧烈波动里追着补。仅供参考，不构成投资建议。")


def _build_china_satcom_advice(advices: list[str], change_percent: Decimal, intraday_amplitude: Decimal, current_price: Decimal) -> None:
    advices.append("建议：中国卫通先看趋势确认，今天默认以观望为主，不把它作为优先补仓对象。仅供参考，不构成投资建议。")
    if current_price >= Decimal("32.00"):
        advices.append("建议：中国卫通目前在 32 元上方附近，先看能否站稳；能稳住再看修复，不稳就别急着加仓。仅供参考，不构成投资建议。")
    else:
        advices.append("建议：中国卫通仍未明显站稳关键位，现阶段更适合继续观察，不建议在这种位置主动补仓。仅供参考，不构成投资建议。")
    if change_percent >= STRONG_UP_DAY_PCT:
        advices.append("建议：中国卫通若只是单日反弹，还不够说明趋势反转；仓位偏重时，可等更强确认后再决定是否继续拿。仅供参考，不构成投资建议。")
    elif change_percent <= WEAK_DOWN_DAY_PCT:
        advices.append("建议：中国卫通当日偏弱时，优先防止回撤扩大，先别想着摊低成本。仅供参考，不构成投资建议。")
    if intraday_amplitude >= HIGH_AMPLITUDE_PCT:
        advices.append("建议：中国卫通盘中分歧较大，如果后面真要处理，也尽量小步分批，不要一次性做满动作。仅供参考，不构成投资建议。")


def _build_cmoc_advice(advices: list[str], change_percent: Decimal, intraday_amplitude: Decimal, current_price: Decimal) -> None:
    advices.append("建议：洛阳钼业当前以修复观察为主，弱势阶段不建议继续补仓，把它当成反弹后再处理的票更合适。仅供参考，不构成投资建议。")
    if current_price >= Decimal("18.50"):
        advices.append("建议：如果洛阳钼业后续能继续反弹到更强位置，更适合考虑减亏处理，而不是马上再加仓摊成本。仅供参考，不构成投资建议。")
    else:
        advices.append("建议：洛阳钼业当前位置仍偏弱，今天先别补，等更像样的企稳或反弹再做决定。仅供参考，不构成投资建议。")
    if change_percent >= STRONG_UP_DAY_PCT:
        advices.append("建议：洛阳钼业即使单日反弹，也先按修复看待；对深套仓来说，先等反弹质量，再决定是不是减仓更实际。仅供参考，不构成投资建议。")
    elif change_percent <= WEAK_DOWN_DAY_PCT:
        advices.append("建议：洛阳钼业当日继续偏弱时，更不适合逆势补仓，先保留现金比继续压重更重要。仅供参考，不构成投资建议。")
    if intraday_amplitude >= HIGH_AMPLITUDE_PCT:
        advices.append("建议：洛阳钼业波动偏大时，说明分歧明显，真要处理也用分批方式，别急着一把定输赢。仅供参考，不构成投资建议。")


def _build_generic_advice(advices: list[str], change_percent: Decimal, intraday_amplitude: Decimal) -> None:
    advices.append("建议：当前先以控制仓位和耐心观察为主，没有明显确认前，不急着补仓。仅供参考，不构成投资建议。")
    if change_percent >= STRONG_UP_DAY_PCT:
        advices.append("建议：当日涨幅偏大时，更适合借反弹做仓位优化，而不是追着加仓。仅供参考，不构成投资建议。")
    elif change_percent <= WEAK_DOWN_DAY_PCT:
        advices.append("建议：当日回撤偏大时，优先等企稳，不要急着抄底补仓。仅供参考，不构成投资建议。")
    if intraday_amplitude >= HIGH_AMPLITUDE_PCT:
        advices.append("建议：盘中振幅较大时，更适合分批处理，不要一笔把计划全打完。仅供参考，不构成投资建议。")
