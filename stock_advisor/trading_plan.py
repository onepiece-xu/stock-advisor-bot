from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .models import PortfolioHolding, PortfolioSnapshot, StockQuote

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TriggerRiskContext:
    """Risk context passed to trigger detection for validation.

    All fields optional — missing fields skip that check.
    """
    change_percent: Decimal = Decimal("0")
    volume_ratio: Decimal | None = None
    step_change_pct: Decimal | None = None
    is_limit_up: bool = False
    is_limit_down: bool = False

    def is_blocked(self) -> tuple[bool, str]:
        """Returns (blocked, reason) if any risk condition blocks the trigger."""
        if self.is_limit_down:
            return True, "跌停板，禁止任何触发执行"
        if self.is_limit_up:
            return True, "涨停板，买入无法成交，卖出可等待开板"
        if self.volume_ratio is not None and self.volume_ratio < Decimal("0.2"):
            return True, f"量比极低（{self.volume_ratio}），流动性不足，禁止触发"
        if self.step_change_pct is not None and abs(self.step_change_pct) > Decimal("8"):
            return True, f"分钟级波动剧烈（{self.step_change_pct}%），暂停触发等待稳定"
        return False, ""


def build_risk_context(quote: StockQuote, history: list[StockQuote] | None = None) -> TriggerRiskContext:
    """Build risk context from a quote and optional history."""
    ctx = TriggerRiskContext(change_percent=quote.change_percent)
    ctx.is_limit_up = quote.change_percent >= Decimal("9.5")
    ctx.is_limit_down = quote.change_percent <= Decimal("-9.5")
    if history and len(history) >= 2:
        prev = history[-2]
        if prev.current_price > 0:
            ctx.step_change_pct = ((quote.current_price - prev.current_price) / prev.current_price * Decimal("100")).quantize(Decimal("0.01"))
    return ctx


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
    # ── Phase 2: state machine ──
    state: str = "armed"        # armed | fired | cooldown | expired | cancelled
    superseded_by: str = ""     # identity of replacement trigger, if any
    _source: str = ""           # origin: auto_profit_sync | exit_plan_sync | debate_sync | "" (manual)
    created_at: str = ""        # ISO datetime when trigger was created


@dataclass(slots=True)
class TriggerHit:
    trigger: TradeTrigger
    current_price: Decimal
    hit_type: str
    quantity: int
    weight_pct: Decimal
    cash_after_trade: Decimal


DEFAULT_TRIGGER_MAP = {
    "003035:南网能源": TradeTrigger(
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
    "603993:洛阳钼业": TradeTrigger(
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
    "601698:中国卫通": TradeTrigger(
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


def build_default_trigger_payload() -> dict[str, list[dict[str, Any]]]:
    return {
        "triggers": [
            {
                "code": trigger.code,
                "name": trigger.name,
                "action": trigger.action,
                "quantity": trigger.quantity,
                "priceMin": str(trigger.price_min),
                "priceMax": str(trigger.price_max),
                "fallbackPrice": str(trigger.fallback_price),
                "note": trigger.note,
                "disableBuy": trigger.disable_buy,
                "state": trigger.state,
                "supersededBy": trigger.superseded_by,
                "_source": trigger._source,
                "_created": trigger.created_at,
            }
            for trigger in DEFAULT_TRIGGER_MAP.values()
        ]
    }


def load_triggers(path: str | Path | None) -> dict[str, TradeTrigger]:
    if path is None:
        return DEFAULT_TRIGGER_MAP.copy()
    trigger_path = Path(path)
    if not trigger_path.exists():
        return DEFAULT_TRIGGER_MAP.copy()
    raw = json.loads(trigger_path.read_text(encoding="utf-8"))
    items = raw.get("triggers", [])
    triggers: dict[str, TradeTrigger] = {}
    for item in items:
        trigger = TradeTrigger(
            code=str(item["code"]),
            name=str(item.get("name", "")),
            action=str(item["action"]),
            quantity=int(item["quantity"]),
            price_min=Decimal(str(item["priceMin"])),
            price_max=Decimal(str(item["priceMax"])),
            fallback_price=Decimal(str(item.get("fallbackPrice", item.get("priceMin", "0")))),
            note=str(item.get("note", "")),
            disable_buy=bool(item.get("disableBuy", False)),
            state=str(item.get("state", "armed")),
            superseded_by=str(item.get("supersededBy", "")),
            _source=str(item.get("_source", "")),
            created_at=str(item.get("_created", "")),
        )
        triggers[f"{trigger.code}:{trigger.name}"] = trigger
    return triggers


def ensure_trigger_file(path: str | Path) -> Path:
    trigger_path = Path(path)
    if trigger_path.exists():
        return trigger_path
    trigger_path.parent.mkdir(parents=True, exist_ok=True)
    trigger_path.write_text(
        json.dumps(build_default_trigger_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return trigger_path


def load_snapshot(path: str | Path) -> PortfolioSnapshot:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return PortfolioSnapshot(
        trade_date=date.fromisoformat(raw["tradeDate"]),
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


def detect_trigger_hit(
    quote: StockQuote,
    snapshot: PortfolioSnapshot,
    triggers: dict[str, TradeTrigger] | None = None,
    risk: TriggerRiskContext | None = None,
) -> TriggerHit | None:
    trigger_map = triggers or DEFAULT_TRIGGER_MAP
    # Support multiple triggers per code (key format: {code}:{name})
    matching = [t for t in trigger_map.values() if t.code == quote.code]
    if not matching:
        return None
    holding = _find_holding(snapshot, quote.code)
    if holding is None or holding.quantity <= 0:
        return None

    # Risk pre-check — block trigger execution in dangerous conditions
    if risk is not None:
        blocked, reason = risk.is_blocked()
        if blocked:
            logger.warning(
                "Trigger blocked for %s price=%s action=multi-trigger reason=%s",
                quote.code, quote.current_price, reason,
            )
            return None

    price = quote.current_price
    # Check each matching trigger for price range hit (first wins)
    for trigger in matching:
        quantity = _dynamic_quantity(trigger, holding, snapshot)
        weight_pct = _holding_weight_pct(holding, snapshot)
        cash_after_trade = snapshot.cash + (price * Decimal(quantity)) if trigger.action == "sell" else snapshot.cash
        if trigger.price_min <= price <= trigger.price_max:
            return TriggerHit(trigger=trigger, current_price=price, hit_type="target_range", quantity=quantity, weight_pct=weight_pct, cash_after_trade=cash_after_trade)
        if price <= trigger.fallback_price:
            return TriggerHit(trigger=trigger, current_price=price, hit_type="fallback", quantity=quantity, weight_pct=weight_pct, cash_after_trade=cash_after_trade)
    return None


def render_trade_instruction(hit: TriggerHit, snapshot: PortfolioSnapshot) -> str:
    trigger = hit.trigger
    qty = hit.quantity
    action_text = "卖出" if trigger.action == "sell" else "继续持有"
    if trigger.action == "hold" and hit.hit_type == "target_range":
        action_text = f"若冲高无力，减 {qty} 股"
    elif trigger.action == "hold":
        action_text = "继续持有，不加仓"
    elif hit.hit_type == "fallback":
        action_text = f"价格跌破防守位，执行减仓 {qty} 股"
    else:
        action_text = f"执行 {action_text} {qty} 股"

    holding = next((h for h in snapshot.holdings if h.code == trigger.code), None)
    current_holding_line = f"当前持仓：{holding.quantity} 股" if holding else "当前持仓：未知"

    return "\n".join(
        [
            f"【盘中交易指令】{trigger.name} {trigger.code}",
            f"当前价：{_fmt(hit.current_price)}",
            f"执行动作：{action_text}",
            f"建议数量：{qty} 股",
            current_holding_line,
            f"持仓占比：{_fmt_pct(hit.weight_pct)}",
            f"执行后预计现金：{_fmt_money(hit.cash_after_trade)}",
            f"触发区间：{_fmt(trigger.price_min)} - {_fmt(trigger.price_max)}",
            f"防守位：{_fmt(trigger.fallback_price)}",
            f"原因：{trigger.note}",
            f"纪律：{'禁止补仓' if trigger.disable_buy else '按原计划执行'}",
            "成交后请直接回传：卖出/买入 代码 数量 成交价",
        ]
    )


def save_snapshot(path: str | Path, snapshot: PortfolioSnapshot) -> None:
    _save_snapshot(path, snapshot)


def apply_trade_fill(
    snapshot_path: str | Path,
    side: str,
    code: str,
    quantity: int,
    price: Decimal,
    *,
    persist: bool = True,
) -> PortfolioSnapshot:
    snapshot = _clone_snapshot(load_snapshot(snapshot_path))
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
    if persist:
        _save_snapshot(snapshot_path, snapshot)
    return snapshot


def build_post_fill_execution_sheet(snapshot: PortfolioSnapshot) -> str:
    lines = [
        "【成交后新执行单】",
        f"总资产：{_fmt_money(snapshot.total_assets)}",
        f"可用现金：{_fmt_money(snapshot.cash)}",
    ]
    for holding in sorted(snapshot.holdings, key=lambda item: _holding_weight_pct(item, snapshot), reverse=True):
        weight = _holding_weight_pct(holding, snapshot)
        pnl = _holding_pnl_pct(holding)
        category, instruction, reason = _post_fill_instruction(holding, snapshot)
        lines.extend(
            [
                f"- {holding.name}({holding.code}) | 分类：{category}",
                f"  指令：{instruction}",
                f"  持仓：{holding.quantity} 股 | 仓位占比：{_fmt_pct(weight)} | 浮盈亏：{_fmt_pct(pnl)}",
                f"  原因：{reason}",
            ]
        )
    lines.append("成交后继续回传：买入/卖出 代码 数量 成交价")
    return "\n".join(lines)


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


def _clone_snapshot(snapshot: PortfolioSnapshot) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        trade_date=snapshot.trade_date,
        total_assets=snapshot.total_assets,
        cash=snapshot.cash,
        holdings=[
            PortfolioHolding(
                name=item.name,
                code=item.code,
                quantity=item.quantity,
                cost_price=item.cost_price,
                current_price=item.current_price,
            )
            for item in snapshot.holdings
        ],
    )


def _find_holding(snapshot: PortfolioSnapshot, code: str) -> PortfolioHolding | None:
    for item in snapshot.holdings:
        if item.code == code:
            return item
    return None


def _dynamic_quantity(trigger: TradeTrigger, holding: PortfolioHolding, snapshot: PortfolioSnapshot) -> int:
    base = trigger.quantity
    if trigger.action != "sell":
        return min(base, holding.quantity)
    weight_pct = _holding_weight_pct(holding, snapshot)
    if weight_pct >= Decimal("30"):
        return min(max(base, 500), holding.quantity)
    if weight_pct >= Decimal("20"):
        return min(max(base, 200), holding.quantity)
    return min(base, holding.quantity)


def _holding_weight_pct(holding: PortfolioHolding, snapshot: PortfolioSnapshot) -> Decimal:
    if snapshot.total_assets <= 0:
        return Decimal("0")
    market_value = holding.current_price * Decimal(holding.quantity)
    return ((market_value / snapshot.total_assets) * Decimal("100")).quantize(Decimal("0.01"))


def _fmt(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def _fmt_pct(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"


def _fmt_money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _holding_pnl_pct(holding: PortfolioHolding) -> Decimal:
    if holding.cost_price <= 0:
        return Decimal("0")
    return (((holding.current_price - holding.cost_price) / holding.cost_price) * Decimal("100")).quantize(Decimal("0.01"))


def _post_fill_instruction(holding: PortfolioHolding, snapshot: PortfolioSnapshot) -> tuple[str, str, str]:
    weight = _holding_weight_pct(holding, snapshot)
    pnl = _holding_pnl_pct(holding)
    if weight >= Decimal("30") and pnl <= Decimal("-8"):
        return ("立即卖", f"若再反弹 1%-2%，继续减 300-500 股", "仓位仍重且浮亏较深，优先继续释放风险")
    if pnl <= Decimal("-10"):
        return ("禁止买", "只减不加，等待下一次反弹窗口", "深度浮亏阶段先控制回撤，不做摊平动作")
    if weight >= Decimal("20"):
        return ("反弹卖", "反弹到预设区间优先减仓，不主动加仓", "仓位不低，先用反弹换现金")
    return ("持有观察", "暂时不动，等更清晰信号", "当前不是最急需处理的仓位")


def _build_exit_plan(holding: PortfolioHolding) -> tuple[str, int, Decimal, Decimal, str]:
    cost = holding.cost_price or Decimal("0")
    current = holding.current_price or Decimal("0")
    quantity = int(holding.quantity or 0)
    if cost <= 0 or current <= 0 or quantity <= 0:
        return ("观察", 0, Decimal("0"), Decimal("0"), "持仓数据不完整")

    pnl_pct = ((current - cost) / cost * Decimal("100")).quantize(Decimal("0.01"))
    reduce_qty = max(100, (quantity // 4 // 100) * 100) if quantity >= 400 else 100
    reduce_qty = min(reduce_qty, quantity)
    target_price = current
    fallback_price = (current * Decimal("0.97")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    if pnl_pct >= Decimal("8"):
        target_price = (current * Decimal("1.02")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return ("止盈计划", reduce_qty, target_price, fallback_price, f"浮盈{pnl_pct}%先兑现一部分，剩余仓位用移动止盈保护利润")
    if pnl_pct >= Decimal("3"):
        target_price = (current * Decimal("1.015")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return ("锁盈计划", reduce_qty, target_price, fallback_price, f"已有浮盈{pnl_pct}%，先锁一部分利润")
    if pnl_pct <= Decimal("-10"):
        target_price = (current * Decimal("1.02")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        fallback_price = (current * Decimal("0.98")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return ("卖点计划", min(max(200, reduce_qty), quantity), target_price, fallback_price, f"浮亏{pnl_pct}%过深，反弹到计划卖点先减仓，跌破防守位执行止损")
    if pnl_pct < Decimal("0"):
        target_price = (current * Decimal("1.03")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return ("卖点计划", reduce_qty, target_price, fallback_price, f"浮亏{pnl_pct}%不再拖延，反弹到计划价先降仓位")
    return ("持有观察", 0, current, fallback_price, f"盈亏{pnl_pct}%不极端，暂不主动卖")


def check_stale_triggers(triggers: dict[str, TradeTrigger], snapshot: PortfolioSnapshot) -> list[str]:
    """Return warnings for triggers whose price ranges are too far from current prices.

    A trigger is stale if:
    - Sell/hold trigger with price_min >15% above current price (unreachable for selling)
    - Sell/hold trigger with current >20% above trigger range (selling too early)
    - Buy trigger with price_max <15% below current price (unreachable for buying)
    - Price below fallback by >15% (stop already breached)
    """
    warnings: list[str] = []
    for _key, trigger in triggers.items():
        holding = _find_holding(snapshot, trigger.code)
        if holding is None or holding.quantity <= 0:
            continue
        current = holding.current_price
        # Sell/hold: trigger range unreachably high
        if trigger.action in ("sell", "hold") and trigger.price_min > 0:
            gap_pct = ((trigger.price_min - current) / current * Decimal("100")).quantize(Decimal("0.1"))
            if gap_pct > Decimal("15"):
                warnings.append(
                    f"- ⚠️ {trigger.name}({trigger.code}) 现价 {_fmt(current)}，卖出触发下限 {_fmt(trigger.price_min)}（+{gap_pct}%），短期难触发，建议下调"
                )
        # Buy: trigger range unreachably low
        if trigger.action == "buy" and trigger.price_max > 0:
            gap_pct = ((current - trigger.price_max) / current * Decimal("100")).quantize(Decimal("0.1"))
            if gap_pct > Decimal("15"):
                warnings.append(
                    f"- ⚠️ {trigger.name}({trigger.code}) 现价 {_fmt(current)}，买入触发上限 {_fmt(trigger.price_max)}（-{gap_pct}%），短期难触发，建议上调"
                )
        if trigger.fallback_price > 0:
            deviation_below = ((current - trigger.fallback_price) / trigger.fallback_price * Decimal("100")).quantize(Decimal("0.1"))
            if current < trigger.price_min and abs(deviation_below) > Decimal("15"):
                warnings.append(
                    f"- ⚠️ {trigger.name}({trigger.code}) 现价 {_fmt(current)}，已大幅跌破防守位 {_fmt(trigger.fallback_price)}（{deviation_below}%），触发单可能已失效"
                )
    return warnings


def save_triggers(path: str | Path, triggers: dict[str, TradeTrigger]) -> None:
    """Write trigger map back to trading-plan.json file."""
    trigger_path = Path(path)
    trigger_path.parent.mkdir(parents=True, exist_ok=True)
    items = []
    for trigger in triggers.values():
        items.append({
            "code": trigger.code,
            "name": trigger.name,
            "action": trigger.action,
            "quantity": trigger.quantity,
            "priceMin": str(trigger.price_min),
            "priceMax": str(trigger.price_max),
            "fallbackPrice": str(trigger.fallback_price),
            "note": trigger.note,
            "disableBuy": trigger.disable_buy,
            "state": trigger.state,
            "supersededBy": trigger.superseded_by,
            "_source": trigger._source,
            "_created": trigger.created_at,
        })
    trigger_path.write_text(
        json.dumps({"triggers": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def remove_orphan_triggers(triggers: dict[str, TradeTrigger], active_codes: set[str]) -> tuple[dict[str, TradeTrigger], int]:
    """Remove triggers for codes that no longer have holdings. Returns (cleaned_triggers, removed_count)."""
    removed = 0
    cleaned: dict[str, TradeTrigger] = {}
    for key, trigger in triggers.items():
        if trigger.code not in active_codes:
            removed += 1
        else:
            cleaned[key] = trigger
    return cleaned, removed


def sync_profit_triggers(
    snapshot: PortfolioSnapshot,
    trigger_path: str | Path,
    *,
    drawdown_pct: Decimal = Decimal("5"),
) -> int:
    """Auto-create trailing take-profit triggers for positions in profit.

    For each holding with profit >= 5%, create/update a sell trigger with:
      - priceMin = current_price (卖单底价)
      - priceMax = current_price * 1.03 (上限留3%空间)
      - fallbackPrice = current_price * 0.97 (回撤3%触发)
      - disableBuy = True (浮盈状态不加仓)

    Existing debate-synced stop-loss triggers are preserved. Only the
    profit-taking triggers for the same code are replaced.

    Returns count of triggers created/updated.
    """
    tp = Path(trigger_path)
    if tp.exists():
        raw = json.loads(tp.read_text(encoding="utf-8"))
        existing_triggers = raw.get("triggers", [])
    else:
        existing_triggers = []

    kept = [
        t for t in existing_triggers
        if not t.get("name", "").endswith("-止盈计划")
    ]

    created = 0
    for h in snapshot.holdings:
        if h.quantity <= 0 or h.cost_price <= 0 or h.current_price <= 0:
            continue
        pnl_pct = (h.current_price - h.cost_price) / h.cost_price * 100
        if pnl_pct < 5:
            continue

        price = h.current_price
        sell_qty = max(100, (h.quantity // 2 // 100) * 100)

        kept.append({
            "code": h.code,
            "name": f"{h.name}-止盈计划",
            "action": "sell",
            "quantity": sell_qty,
            "priceMin": str(price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "priceMax": str((price * Decimal("1.03")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "fallbackPrice": str((price * (Decimal("1") - drawdown_pct / Decimal("100"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "note": f"自动止盈计划：浮盈{pnl_pct:.1f}%，卖{sell_qty}股先锁利润，剩余仓位继续移动止盈",
            "disableBuy": True,
            "_source": "auto_profit_sync",
            "_created": datetime.now().isoformat(),
        })
        created += 1

    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(
        json.dumps({"triggers": kept}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return created


def sync_exit_plan_triggers(
    snapshot: PortfolioSnapshot,
    trigger_path: str | Path,
    *,
    max_single_position_pct: float = 35.0,
) -> int:
    """Sync exit plans for underwater positions into executable sell triggers."""
    tp = Path(trigger_path)
    if tp.exists():
        raw = json.loads(tp.read_text(encoding="utf-8"))
        existing_triggers = raw.get("triggers", [])
    else:
        existing_triggers = []

    debate_codes = {
        str(t.get("code", ""))
        for t in existing_triggers
        if t.get("_source") == "debate_sync" and str(t.get("action", "")) == "sell"
    }

    kept = [
        t for t in existing_triggers
        if t.get("_source") != "exit_plan_sync"
    ]

    created = 0
    for h in snapshot.holdings:
        if h.quantity <= 0 or h.cost_price <= 0 or h.current_price <= 0:
            continue
        if h.code in debate_codes:
            continue
        pnl_pct = (h.current_price - h.cost_price) / h.cost_price * 100
        if pnl_pct >= 0:
            continue

        plan_action, plan_qty, plan_target_price, plan_fallback_price, plan_reason = _build_exit_plan(h)
        if plan_qty <= 0 or plan_action == "持有观察":
            continue

        price_min = max(h.current_price, plan_target_price - Decimal("0.10")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        price_max = (plan_target_price + Decimal("0.10")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        fallback = plan_fallback_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        qty = min(plan_qty, h.quantity)
        kept.append({
            "code": h.code,
            "name": f"{h.name}-卖点计划",
            "action": "sell",
            "quantity": qty,
            "priceMin": str(price_min),
            "priceMax": str(price_max),
            "fallbackPrice": str(fallback),
            "note": f"自动卖点计划：{plan_reason}",
            "disableBuy": True,
            "_source": "exit_plan_sync",
            "_created": datetime.now().isoformat(),
        })
        created += 1

    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(
        json.dumps({"triggers": kept}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return created
