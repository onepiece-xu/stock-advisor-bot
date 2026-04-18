from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from .models import PortfolioHolding, PortfolioSnapshot, StockQuote


@dataclass(slots=True)
class TradeTrigger:
    code: str
    name: str
    action: str
    quantity: int
    price_min: Decimal
    price_max: Decimal
    fallback_price: Decimal
    note: str
    disable_buy: bool = False


@dataclass(slots=True)
class TriggerHit:
    trigger: TradeTrigger
    current_price: Decimal
    hit_type: str


DEFAULT_TRIGGERS = {
    "003035": TradeTrigger(
        code="003035",
        name="南网能源",
        action="sell",
        quantity=500,
        price_min=Decimal("7.95"),
        price_max=Decimal("8.10"),
        fallback_price=Decimal("7.80"),
        note="反弹进入减仓带先卖 500 股，若跌回弱承接位下方再执行第二笔减仓",
        disable_buy=True,
    ),
    "603993": TradeTrigger(
        code="603993",
        name="洛阳钼业",
        action="sell",
        quantity=200,
        price_min=Decimal("20.50"),
        price_max=Decimal("20.80"),
        fallback_price=Decimal("20.00"),
        note="反弹到减仓带先卖 200 股，若重新跌回 20 下方，继续禁止补仓",
        disable_buy=True,
    ),
    "601698": TradeTrigger(
        code="601698",
        name="中国卫通",
        action="hold",
        quantity=100,
        price_min=Decimal("34.40"),
        price_max=Decimal("34.60"),
        fallback_price=Decimal("33.20"),
        note="默认持有观察，只有冲到 34.5 附近明显无力时才考虑减 100 股",
        disable_buy=True,
    ),
}


def load_snapshot(path: str | Path) -> PortfolioSnapshot:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return PortfolioSnapshot(
        trade_date=__import__("datetime").date.fromisoformat(raw["tradeDate"]),
        total_assets=Decimal(str(raw.get("totalAssets", 0))),
        cash=Decimal(str(raw.get("cash", 0))),
        holdings=[
            PortfolioHolding(
                name=item["name"],
                code=str(item["code"]),
                quantity=int(item["quantity"]),
                cost_price=Decimal(str(item.get("costPrice", 0))),
                current_price=Decimal(str(item.get("currentPrice", 0))),
            )
            for item in raw.get("holdings", [])
        ],
    )


def detect_trigger_hit(quote: StockQuote, snapshot: PortfolioSnapshot) -> TriggerHit | None:
    trigger = DEFAULT_TRIGGERS.get(quote.code)
    if trigger is None:
        return None
    holding = _find_holding(snapshot, quote.code)
    if holding is None or holding.quantity <= 0:
        return None
    price = quote.current_price
    if trigger.price_min <= price <= trigger.price_max:
        return TriggerHit(trigger=trigger, current_price=price, hit_type="target_range")
    if price <= trigger.fallback_price:
        return TriggerHit(trigger=trigger, current_price=price, hit_type="fallback")
    return None


def render_trade_instruction(hit: TriggerHit, snapshot: PortfolioSnapshot) -> str:
    trigger = hit.trigger
    holding = _find_holding(snapshot, trigger.code)
    qty = min(trigger.quantity, holding.quantity if holding else trigger.quantity)
    action_text = "卖出" if trigger.action == "sell" else "继续持有"
    if trigger.action == "hold" and hit.hit_type == "target_range":
        action_text = f"若冲高无力，减 {qty} 股"
    elif trigger.action == "hold":
        action_text = "继续持有，不加仓"
    elif hit.hit_type == "fallback":
        action_text = f"价格跌破防守位，执行减仓 {qty} 股"
    else:
        action_text = f"执行 {action_text} {qty} 股"

    return "\n".join(
        [
            f"【盘中交易指令】{trigger.name} {trigger.code}",
            f"当前价：{_fmt(hit.current_price)}",
            f"指令：{action_text}",
            f"触发区间：{_fmt(trigger.price_min)} - {_fmt(trigger.price_max)}",
            f"防守位：{_fmt(trigger.fallback_price)}",
            f"原因：{trigger.note}",
            f"补充：{'禁止补仓' if trigger.disable_buy else '按原计划执行'}",
            "收到成交结果后，请回传：买入/卖出 股票 数量 成交价",
        ]
    )


def apply_trade_fill(snapshot_path: str | Path, side: str, code: str, quantity: int, price: Decimal) -> PortfolioSnapshot:
    snapshot = load_snapshot(snapshot_path)
    holding = _find_holding(snapshot, code)
    if holding is None:
        raise RuntimeError(f"持仓中没有 {code}")
    if side == "sell":
        if quantity > holding.quantity:
            raise RuntimeError(f"卖出数量超过持仓: {quantity} > {holding.quantity}")
        proceeds = price * Decimal(quantity)
        snapshot.cash += proceeds
        holding.quantity -= quantity
        if holding.quantity == 0:
            snapshot.holdings = [item for item in snapshot.holdings if item.code != code]
    elif side == "buy":
        cost = price * Decimal(quantity)
        snapshot.cash -= cost
        total_cost = holding.cost_price * Decimal(holding.quantity) + cost
        holding.quantity += quantity
        if holding.quantity > 0:
            holding.cost_price = (total_cost / Decimal(holding.quantity)).quantize(Decimal("0.0001"))
        holding.current_price = price
    else:
        raise RuntimeError(f"不支持的成交方向: {side}")

    total_market = sum(item.current_price * Decimal(item.quantity) for item in snapshot.holdings)
    snapshot.total_assets = (snapshot.cash + total_market).quantize(Decimal("0.01"))
    for item in snapshot.holdings:
        if item.code == code:
            item.current_price = price
    _save_snapshot(snapshot_path, snapshot)
    return snapshot


def _save_snapshot(path: str | Path, snapshot: PortfolioSnapshot) -> None:
    payload = {
        "tradeDate": snapshot.trade_date.isoformat(),
        "totalAssets": float(snapshot.total_assets),
        "cash": float(snapshot.cash),
        "holdings": [
            {
                "name": item.name,
                "code": item.code,
                "quantity": item.quantity,
                "costPrice": float(item.cost_price),
                "currentPrice": float(item.current_price),
            }
            for item in snapshot.holdings
        ],
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _find_holding(snapshot: PortfolioSnapshot, code: str) -> PortfolioHolding | None:
    for item in snapshot.holdings:
        if item.code == code:
            return item
    return None


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))
