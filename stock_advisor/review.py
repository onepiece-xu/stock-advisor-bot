from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import AppConfig
from .market_hours import MARKET_TZ
from .portfolio import load_snapshot as load_portfolio_snapshot
from .storage import connect_db, fetch_daily_review_snapshot, fetch_latest_trade_date


@dataclass(slots=True)
class ReviewArtifact:
    trade_date: date
    title: str
    body: str
    saved_path: Path


def build_close_review(config: AppConfig, *, trade_date: date | None = None) -> ReviewArtifact:
    if trade_date is None:
        trade_date = datetime.now(MARKET_TZ).date()
    conn = connect_db(config.storage.sqlite_path)
    requested_trade_date = trade_date
    items = fetch_daily_review_snapshot(conn, requested_trade_date.isoformat())
    if not items:
        latest_trade_date = fetch_latest_trade_date(conn)
        if latest_trade_date:
            trade_date = date.fromisoformat(latest_trade_date)
            items = fetch_daily_review_snapshot(conn, latest_trade_date)
    title = f"收盘复盘 {trade_date.isoformat()}"
    body = _render_review_body(config, trade_date, items, requested_trade_date=requested_trade_date)
    saved_path = _save_review(config.review.data_dir, trade_date, body)
    return ReviewArtifact(trade_date=trade_date, title=title, body=body, saved_path=saved_path)


def should_send_close_review_now(config: AppConfig, *, now: datetime | None = None) -> bool:
    if not config.review.enabled or not config.review.auto_notify:
        return False
    if now is None:
        now = datetime.now(MARKET_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=MARKET_TZ)
    else:
        now = now.astimezone(MARKET_TZ)
    if now.weekday() >= 5:
        return False
    cutoff = time(config.review.send_after_hour, config.review.send_after_minute)
    return now.time() >= cutoff


def already_sent_close_review(config: AppConfig, trade_date: date) -> bool:
    state = _load_review_state(config.review.data_dir)
    return state.get("last_sent_trade_date") == trade_date.isoformat()


def mark_close_review_sent(config: AppConfig, trade_date: date) -> None:
    state_path = _review_state_path(config.review.data_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "last_sent_trade_date": trade_date.isoformat(),
                "updated_at": datetime.now(MARKET_TZ).isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _render_review_body(config: AppConfig, trade_date: date, items: list[dict], *, requested_trade_date: date) -> str:
    lines = [f"【收盘复盘】{trade_date.isoformat()}"]
    if trade_date != requested_trade_date:
        lines.append(f"说明: 当日暂无落库行情，已回退到最近交易日 {trade_date.isoformat()}")
    if not items:
        lines.append("今日暂无落库行情数据，未生成复盘明细。")
        lines.append("建议确认 monitor-daemon 是否正常运行。")
        return "\n".join(lines)

    scores = [Decimal(str(item["score"])) for item in items if item["score"] is not None]
    avg_score = _avg(scores)
    positive = max(items, key=lambda item: item["change_percent"])
    negative = min(items, key=lambda item: item["change_percent"])
    buy_like = [item["code"] for item in items if item["action"] == "buy"]
    reduce_like = [item["code"] for item in items if item["action"] in {"reduce", "avoid"}]

    lines.extend(
        [
            f"覆盖标的: {len(items)}",
            f"平均分: {_fmt_decimal(avg_score)}" if avg_score is not None else "平均分: N/A",
            f"偏强: {positive['code']} {positive['name']} {_signed(positive['change_percent'])}%",
            f"偏弱: {negative['code']} {negative['name']} {_signed(negative['change_percent'])}%",
            f"关注买点: {', '.join(buy_like) if buy_like else '暂无'}",
            f"优先减仓: {', '.join(reduce_like) if reduce_like else '暂无'}",
            "",
            "【标的复盘】",
        ]
    )

    for item in items:
        lines.append(
            f"- {item['code']} {item['name']} | 收盘 {_fmt_float(item['current_price'])} | 涨跌 {_signed(item['change_percent'])}% | 动作 {item['action']} | 评分 {_fmt_optional(item['score'])}"
        )
        lines.append(f"  状态 {item['regime']} / {item['confidence']} / {item['signal_level']}")
        if item["trade_advice"]:
            lines.append(f"  建议 {item['trade_advice']} | 仓位 {item['trade_size_hint']}")
        if item["entry_note"]:
            lines.append(f"  处理 {item['entry_note']}")
        reason = "；".join(item["rationale"][:2]) if item["rationale"] else "暂无明显理由"
        lines.append(f"  理由 {reason}")
        if item["risk_flags"]:
            lines.append(f"  风险 {'；'.join(item['risk_flags'][:2])}")

    portfolio_path = config.storage.sqlite_path.resolve().parent.parent / "portfolio-snapshot.json"
    if portfolio_path.exists():
        lines.extend(["", "【持仓复盘】"])
        lines.extend(_render_portfolio_section(portfolio_path, items))

    lines.extend(["", "【结论】"])
    if avg_score is not None and avg_score >= Decimal("58"):
        lines.append("- 今日整体评分偏中性偏强，优先保留强势、弱势只做反弹处理。")
    elif avg_score is not None and avg_score <= Decimal("42"):
        lines.append("- 今日整体评分偏弱，控制仓位与现金比盲目抄底更重要。")
    else:
        lines.append("- 今日整体仍是分化市况，按个股评分和仓位纪律执行。")
    lines.append("- 仅供参考，不构成投资建议。")
    return "\n".join(lines)


def _render_portfolio_section(snapshot_path: Path, items: list[dict]) -> list[str]:
    snapshot = load_portfolio_snapshot(snapshot_path)
    item_map = {item["code"]: item for item in items}
    total_assets = snapshot.total_assets if snapshot.total_assets > 0 else Decimal("0")
    lines = [
        f"总资产: {_fmt_decimal(snapshot.total_assets)}",
        f"现金: {_fmt_decimal(snapshot.cash)}",
    ]
    for holding in snapshot.holdings:
        latest = item_map.get(holding.code)
        latest_price = Decimal(str(latest["current_price"])) if latest else holding.current_price
        pnl = _pnl_pct(holding.cost_price, latest_price)
        market_value = latest_price * Decimal(holding.quantity)
        weight = Decimal("0")
        if total_assets > 0:
            weight = (market_value / total_assets * Decimal("100")).quantize(Decimal("0.01"))
        action = latest["action"] if latest else "unknown"
        lines.append(
            f"- {holding.name}({holding.code}) | 仓位 {_fmt_decimal(weight)}% | 浮盈亏 {_signed_decimal(pnl)}% | 最新动作 {action}"
        )
    return lines


def _save_review(data_dir: Path, trade_date: date, body: str) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{trade_date.isoformat()}-close-review.txt"
    path.write_text(body, encoding="utf-8")
    return path


def _review_state_path(data_dir: Path) -> Path:
    return data_dir / "close-review-state.json"


def _load_review_state(data_dir: Path) -> dict:
    path = _review_state_path(data_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _avg(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    total = sum(values, Decimal("0"))
    return (total / Decimal(len(values))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _fmt_optional(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.0f}"


def _fmt_float(value: float) -> str:
    return f"{value:.3f}"


def _fmt_decimal(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _signed(value: float) -> str:
    return f"+{value:.2f}" if value > 0 else f"{value:.2f}"


def _signed_decimal(value: Decimal) -> str:
    scaled = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"+{scaled}" if scaled > 0 else str(scaled)


def _pnl_pct(cost_price: Decimal, current_price: Decimal) -> Decimal:
    if cost_price <= 0:
        return Decimal("0")
    return (((current_price - cost_price) / cost_price) * Decimal("100")).quantize(Decimal("0.01"))
