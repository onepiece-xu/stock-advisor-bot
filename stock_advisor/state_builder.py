from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from .portfolio import load_snapshot
from .trading_plan import load_triggers
from .trading_state import HoldingState, InstructionState, TradingState


def build_trading_state(snapshot_path: str | Path, trigger_path: str | Path, briefing_path: str | Path) -> TradingState:
    snapshot = load_snapshot(snapshot_path)
    triggers = load_triggers(trigger_path)
    briefing_file = Path(briefing_path)
    briefing = json.loads(briefing_file.read_text(encoding="utf-8")) if briefing_file.exists() else {}

    holdings = []
    active_codes = set()
    for item in snapshot.holdings:
        if item.quantity <= 0:
            continue
        pnl_pct = Decimal("0")
        if item.cost_price > 0:
            pnl_pct = ((item.current_price - item.cost_price) / item.cost_price * Decimal("100")).quantize(Decimal("0.0001"))
        holdings.append(
            HoldingState(
                code=item.code,
                name=item.name,
                quantity=item.quantity,
                cost_price=item.cost_price,
                current_price=item.current_price,
                pnl_pct=pnl_pct,
                market_value=(item.current_price * item.quantity).quantize(Decimal("0.01")),
            )
        )
        active_codes.add(item.code)

    active_instructions: list[InstructionState] = []
    orphan_instructions: list[InstructionState] = []
    for trigger in triggers.values():
        instruction = InstructionState(
            code=trigger.code,
            name=trigger.name,
            action=trigger.action,
            quantity=trigger.quantity,
            trigger_low=trigger.price_min,
            trigger_high=trigger.price_max,
            fallback_price=trigger.fallback_price,
            reason=trigger.note,
        )
        if trigger.code in active_codes:
            active_instructions.append(instruction)
        else:
            orphan_instructions.append(instruction)

    return TradingState(
        trade_date=snapshot.trade_date,
        generated_at=datetime.now(),
        total_assets=snapshot.total_assets,
        cash=snapshot.cash,
        holdings=holdings,
        active_instructions=active_instructions,
        orphan_instructions=orphan_instructions,
        briefing_date=str(briefing.get("date", "")),
        briefing_generated_at=str(briefing.get("generated_at", "")),
        briefing_summary=str(briefing.get("summary", "") or ""),
    )


# ── Auto-generated instruction sources (replaced on each sync) ──

_AUTO_SOURCES = {"auto_profit_sync", "exit_plan_sync"}


def generate_and_sync_triggers(
    snapshot_path: str | Path,
    trigger_path: str | Path,
    *,
    profit_drawdown_pct: Decimal = Decimal("5"),
    exit_max_single_position_pct: float = 35.0,
) -> tuple[int, int]:
    """Single entry-point: generate profit & exit-plan instructions, sync to trading_plan.json.

    Replaces the old scattered sync_profit_triggers / sync_exit_plan_triggers calls.
    Returns (profit_count, exit_plan_count).
    """
    snapshot = load_snapshot(snapshot_path)
    tp = Path(trigger_path)

    # Load existing triggers, strip auto-generated ones
    if tp.exists():
        raw = json.loads(tp.read_text(encoding="utf-8"))
        existing_triggers = raw.get("triggers", [])
    else:
        existing_triggers = []

    kept = [t for t in existing_triggers if t.get("_source") not in _AUTO_SOURCES]

    # ── Generate profit instructions ──
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
            "fallbackPrice": str((price * (Decimal("1") - profit_drawdown_pct / Decimal("100"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "note": f"自动止盈计划：浮盈{pnl_pct:.1f}%，卖{sell_qty}股先锁利润，剩余仓位继续移动止盈",
            "disableBuy": True,
            "state": "armed",
            "_source": "auto_profit_sync",
            "_created": datetime.now().isoformat(),
        })

    # ── Generate exit-plan instructions ──
    debate_codes = {
        str(t.get("code", ""))
        for t in existing_triggers
        if t.get("_source") == "debate_sync" and str(t.get("action", "")) == "sell"
    }

    for h in snapshot.holdings:
        if h.quantity <= 0 or h.cost_price <= 0 or h.current_price <= 0:
            continue
        if h.code in debate_codes:
            continue
        pnl_pct = (h.current_price - h.cost_price) / h.cost_price * 100
        if pnl_pct >= 0:
            continue

        from .trading_plan import _build_exit_plan
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
            "state": "armed",
            "_source": "exit_plan_sync",
            "_created": datetime.now().isoformat(),
        })

    # ── Write back ──
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(
        json.dumps({"triggers": kept}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Count from final list
    profit_count = sum(1 for t in kept if t.get("_source") == "auto_profit_sync")
    exit_count = sum(1 for t in kept if t.get("_source") == "exit_plan_sync")
    return profit_count, exit_count
