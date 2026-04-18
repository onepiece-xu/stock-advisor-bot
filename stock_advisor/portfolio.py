from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from .models import PortfolioHolding, PortfolioSnapshot


@dataclass(slots=True)
class AdviceItem:
    priority: int
    title: str
    detail: str


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


def save_snapshot(snapshot: PortfolioSnapshot, data_dir: Path) -> Path:
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
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


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

    lines.extend(["", "【持仓明细】"])
    for holding in current.holdings:
        lines.append(
            f"- {holding.name}({holding.code})：{holding.quantity}股，成本 {_format_money(holding.cost_price)}，现价 {_format_money(holding.current_price)}，浮盈亏 {_format_percent(_pnl_percent(holding))}"
        )

    if previous is not None:
        lines.extend(["", "【较昨日变化】"])
        for line in _build_diff_lines(current, previous):
            lines.append(f"- {line}")

    lines.extend(["", "【建议动作】"])
    advice_items = sorted((_advice_for_holding(h) for h in current.holdings), key=lambda x: x.priority)
    for item in advice_items:
        lines.append(item.title)
        lines.append(item.detail)

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


def _advice_for_holding(holding: PortfolioHolding) -> AdviceItem:
    if holding.name == "南网能源":
        return AdviceItem(1, "1. 南网能源：优先减仓释放现金", "   先减仓 1000 股；若后续反弹到 10.20 附近，可再减 500 股；若明显转弱跌破 9.00，先不补仓，重新观察")
    if holding.name == "洛阳钼业":
        return AdviceItem(2, "2. 洛阳钼业：等企稳再决定", "   仅在 17.00 附近且止跌企稳时，考虑补 200~300 股；反弹到 19.50 附近可减 300 股，21.00 附近再减 300 股")
    if holding.name == "中国卫通":
        return AdviceItem(3, "3. 中国卫通：持有观察，设纪律位", "   若跌破 30.50 可减 150 股；若反弹到 33.30~33.80 区间，可减 150 股")
    return AdviceItem(9, f"9. {holding.name}：暂时观察", "   暂无预设规则，结合收盘价、仓位和次日强弱再判断")


def _pnl_percent(holding: PortfolioHolding) -> Decimal:
    if holding.cost_price <= 0:
        return Decimal("0")
    return (((holding.current_price - holding.cost_price) / holding.cost_price) * Decimal("100")).quantize(Decimal("0.01"))


def _position_ratio(snapshot: PortfolioSnapshot) -> Decimal:
    if snapshot.total_assets <= 0:
        return Decimal("0")
    return (((snapshot.total_assets - snapshot.cash) / snapshot.total_assets) * Decimal("100")).quantize(Decimal("0.01"))


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
