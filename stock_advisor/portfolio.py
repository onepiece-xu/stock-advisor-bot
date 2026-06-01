from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from .models import ActionCandidate, PortfolioHolding, PortfolioSnapshot, build_position_exit_plan


@dataclass(slots=True)
class AdviceItem:
    priority: int
    title: str
    detail: str
    candidates: list[ActionCandidate]


def load_snapshot(path: str | Path) -> PortfolioSnapshot:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    # tradeDate is optional — fall back to updated field or today
    trade_date_str = raw.get("tradeDate") or raw.get("updated", "").split("T")[0]
    trade_date = date.fromisoformat(trade_date_str) if trade_date_str else date.today()
    return PortfolioSnapshot(
        trade_date=trade_date,
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


def save_snapshot(snapshot: PortfolioSnapshot, data_dir: Path) -> Path:
    import hashlib
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{snapshot.trade_date.isoformat()}.json"
    payload = {
        "tradeDate": snapshot.trade_date.isoformat(),
        "totalAssets": float(snapshot.total_assets),
        "cash": float(snapshot.cash),
        "holdings": [
            {
                "name": h.name,
                "code": h.code,
                "quantity": h.quantity,
                "costPrice": float(h.cost_price),
                "currentPrice": float(h.current_price),
            }
            for h in snapshot.holdings
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    checksum = hashlib.sha256(raw.encode()).hexdigest()[:12]
    payload["_checksum"] = checksum  # Store in payload for load verification
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def generate_portfolio_report(snapshot_path: str | Path, data_dir: Path) -> tuple[PortfolioSnapshot, Path, str]:
    snapshot = load_snapshot(snapshot_path)
    previous = load_previous_snapshot(data_dir, snapshot.trade_date)
    saved_path = save_snapshot(snapshot, data_dir)
    report = build_daily_report(snapshot, previous)
    return snapshot, saved_path, report


def load_previous_snapshot(data_dir: Path, before_date: date) -> PortfolioSnapshot | None:
    if not data_dir.exists():
        return None
    candidates = sorted(data_dir.glob("*.json"))
    valid = []
    for file in candidates:
        try:
            snap_date = date.fromisoformat(file.stem)
        except ValueError:
            continue
        if snap_date < before_date:
            valid.append((snap_date, file))
    if not valid:
        return None
    return load_snapshot(valid[-1][1])


def find_holding(snapshot: PortfolioSnapshot | None, code: str) -> PortfolioHolding | None:
    if snapshot is None:
        return None
    for holding in snapshot.holdings:
        if holding.code == code:
            return holding
    return None


def compute_cash_ratio(snapshot) -> Decimal | None:
    if snapshot is None or snapshot.total_assets <= 0:
        return None
    return (snapshot.cash / snapshot.total_assets).quantize(Decimal("0.0001"))


def compute_position_ratio(snapshot, holding, current_price: Decimal) -> Decimal | None:
    if snapshot is None or snapshot.total_assets <= 0 or holding is None or holding.quantity <= 0:
        return None
    position_value = Decimal(str(holding.quantity)) * current_price
    return (position_value / snapshot.total_assets).quantize(Decimal("0.0001"))


def build_daily_report(current: PortfolioSnapshot, previous: PortfolioSnapshot | None) -> str:
    lines = [
        "【收盘持仓建议】",
        f"日期：{current.trade_date.isoformat()}",
        f"总资产：{_format_money(current.total_assets)}",
        f"可用现金：{_format_money(current.cash)}",
        f"当前仓位：{_format_percent(_position_ratio(current))}",
    ]
    if previous is not None:
        lines.append(f"较昨日总资产变化：{_format_signed_money(current.total_assets - previous.total_assets)}")
        lines.append(f"较昨日现金变化：{_format_signed_money(current.cash - previous.cash)}")

    active_holdings = [h for h in current.holdings if h.quantity > 0]
    closed_holdings = [h for h in current.holdings if h.quantity <= 0]

    lines.extend(["", "【持仓明细】"])
    for holding in active_holdings:
        lines.append(
            f"- {holding.name}({holding.code})：{holding.quantity}股，成本 {_format_money(holding.cost_price)}，现价 {_format_money(holding.current_price)}，浮盈亏 {_format_percent(_pnl_percent(holding))}"
        )
    for holding in closed_holdings:
        lines.append(
            f"- {holding.name}({holding.code})：已清仓（成本 {_format_money(holding.cost_price)}，现价 {_format_money(holding.current_price)}）"
        )

    if previous is not None:
        lines.extend(["", "【较昨日变化】"])
        for line in _build_diff_lines(current, previous):
            lines.append(f"- {line}")

    lines.extend(["", "【执行单】"])
    advice_items = sorted((_advice_for_holding(h, current) for h in active_holdings), key=lambda x: x.priority)
    for item in advice_items:
        lines.append(item.title)
        lines.append(item.detail)
        for candidate in item.candidates:
            lines.append(
                f"  - 指令:{candidate.action} | 风险:{candidate.risk_level} | 条件:{candidate.trigger} | 原因:{candidate.reason}"
            )

    lines.extend(["", "【原则】"])
    ratio = _position_ratio(current)
    if ratio >= Decimal("90"):
        lines.append("- 仓位仍然很高，优先考虑降低仓位、保留现金。")
    elif ratio >= Decimal("75"):
        lines.append("- 仓位偏高，继续分批调整，不要一次性满上。")
    else:
        lines.append("- 仓位相对可控，按触发条件执行即可。")
    lines.append("- 补仓必须等企稳，不要边跌边补。")
    lines.append("- 仅供参考，不构成投资建议。")
    return "\n".join(lines)


def _build_diff_lines(current: PortfolioSnapshot, previous: PortfolioSnapshot) -> list[str]:
    previous_map = {holding.code: holding for holding in previous.holdings}
    lines: list[str] = []
    for current_holding in current.holdings:
        previous_holding = previous_map.get(current_holding.code)
        if previous_holding is None:
            lines.append(f"{current_holding.name} 为新增持仓")
            continue
        quantity_diff = current_holding.quantity - previous_holding.quantity
        price_diff = current_holding.current_price - previous_holding.current_price
        if quantity_diff != 0 or price_diff != 0:
            lines.append(
                f"{current_holding.name}：数量变化 {_format_signed_int(quantity_diff)} 股，价格变化 {_format_signed_money(price_diff)}"
            )
    return lines or ["持仓数量未变化，主要是价格浮动"]


def _advice_for_holding(holding: PortfolioHolding, snapshot: PortfolioSnapshot) -> AdviceItem:
    pnl_pct = _pnl_percent(holding)
    weight_pct = _holding_weight_percent(holding, snapshot)
    exit_plan = build_position_exit_plan(holding)
    candidates: list[ActionCandidate] = []

    if weight_pct >= Decimal("40"):
        candidates.append(ActionCandidate("reduce", "单票仓位过高，优先兑现部分仓位，回收现金。", f"减仓优先区 { _format_money(exit_plan.first_take_profit) } 附近", "high"))
    if pnl_pct <= Decimal("-10"):
        candidates.append(ActionCandidate("reduce", "弱势仓先按计划卖点减仓。", f"反弹看 { _format_money(exit_plan.first_take_profit) }，止损 { _format_money(exit_plan.stop_loss) }", "high"))
    elif pnl_pct < Decimal("0"):
        candidates.append(ActionCandidate("hold", "先等计划卖点，不追求回本，守住止损。", f"反弹看 { _format_money(exit_plan.first_take_profit) }，止损 { _format_money(exit_plan.stop_loss) }", "medium"))
    else:
        candidates.append(ActionCandidate("hold", "已有利润，优先按移动止盈计划处理。", f"先看 { _format_money(exit_plan.first_take_profit) }，移动止盈 {exit_plan.trailing_take_profit_pct}%", "low"))

    if holding.current_price >= holding.cost_price and holding.cost_price > 0:
        candidates.append(ActionCandidate("reduce", "价格回到成本上方后，先兑现一部分，剩余仓位用移动止盈。", f"强势目标 { _format_money(exit_plan.final_take_profit) }", "medium"))

    top_action = candidates[0].action
    title = f"- {holding.name}({holding.code})：执行优先级 {top_action}"
    detail = (
        f"  浮盈亏 {_format_percent(pnl_pct)}，仓位占比 {_format_percent(weight_pct)}，"
        f"止损 {_format_money(exit_plan.stop_loss)}，第一卖点 {_format_money(exit_plan.first_take_profit)}，"
        f"移动止盈 {exit_plan.trailing_take_profit_pct}%"
    )
    priority = _priority_for_candidates(candidates)
    return AdviceItem(priority=priority, title=title, detail=detail, candidates=candidates)


def _priority_for_candidates(candidates: list[ActionCandidate]) -> int:
    ranking = {"reduce": 1, "avoid": 2, "hold": 3, "buy": 4}
    return min(ranking.get(candidate.action, 9) for candidate in candidates)


def _pnl_percent(holding: PortfolioHolding) -> Decimal:
    if holding.cost_price <= 0:
        return Decimal("0")
    return (((holding.current_price - holding.cost_price) / holding.cost_price) * Decimal("100")).quantize(Decimal("0.01"))


def _position_ratio(snapshot: PortfolioSnapshot) -> Decimal:
    if snapshot.total_assets <= 0:
        return Decimal("0")
    return (((snapshot.total_assets - snapshot.cash) / snapshot.total_assets) * Decimal("100")).quantize(Decimal("0.01"))


def _holding_weight_percent(holding: PortfolioHolding, snapshot: PortfolioSnapshot) -> Decimal:
    if snapshot.total_assets <= 0:
        return Decimal("0")
    market_value = holding.current_price * Decimal(holding.quantity)
    return ((market_value / snapshot.total_assets) * Decimal("100")).quantize(Decimal("0.01"))


def _format_money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _format_signed_money(value: Decimal) -> str:
    scaled = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    prefix = "+" if scaled > 0 else ""
    return f"{prefix}{scaled}"


def _format_percent(value: Decimal) -> str:
    scaled = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    prefix = "+" if scaled > 0 else ""
    return f"{prefix}{scaled}%"


def _format_signed_int(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)
