from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .logging_utils import get_logger
from .opportunity_scanner import Candidate, build_trade_idea


logger = get_logger(__name__)
JOURNAL_FILE = "opportunity_journal.jsonl"


@dataclass(slots=True)
class OpportunityOutcome:
    trade_date: date
    code: str
    name: str
    score: Decimal
    entry_type: str
    entry_price: Decimal
    next_day_close: Decimal | None
    next_day_high: Decimal | None
    day3_close: Decimal | None
    day3_high: Decimal | None


def record_daily_opportunities(data_dir: Path, trade_date: date, candidates: list[Candidate], config) -> int:
    if not candidates:
        return 0

    journal_path = data_dir / JOURNAL_FILE
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    existing_keys = _load_existing_keys(journal_path)
    written = 0

    with journal_path.open("a", encoding="utf-8") as fh:
        for candidate in candidates:
            key = f"{trade_date.isoformat()}:{candidate.code}"
            if key in existing_keys:
                continue
            idea = build_trade_idea(candidate, config)
            record = {
                "trade_date": trade_date.isoformat(),
                "code": candidate.code,
                "name": candidate.name,
                "score": str(candidate.composite_score),
                "current_price": str(candidate.current_price),
                "change_pct": str(candidate.change_pct),
                "entry_type": idea.entry_plan.entry_type,
                "buy_zone_low": str(idea.entry_plan.buy_zone_low),
                "buy_zone_high": str(idea.entry_plan.buy_zone_high),
                "trigger_price": str(idea.entry_plan.trigger_price),
                "stop_loss": str(idea.exit_plan.stop_loss),
                "first_take_profit": str(idea.exit_plan.first_take_profit),
                "final_take_profit": str(idea.exit_plan.final_take_profit),
                "trailing_take_profit_pct": str(idea.exit_plan.trailing_take_profit_pct),
                "thesis": candidate.flags[:4],
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1
    return written


def render_recent_opportunity_validation(
    conn: sqlite3.Connection,
    data_dir: Path,
    *,
    as_of_date: date,
    lookback_days: int = 7,
) -> str:
    outcomes = evaluate_recent_opportunities(conn, data_dir, as_of_date=as_of_date, lookback_days=lookback_days)
    if not outcomes:
        return "📘 **主动机会验证**: 暂无已进入次日/第三日观察窗口的机会单。"

    next_day_available = [o for o in outcomes if o.next_day_close is not None]
    day3_available = [o for o in outcomes if o.day3_close is not None]
    next_day_red = [o for o in next_day_available if _pct_change(o.entry_price, o.next_day_close) > Decimal("0")]
    day3_red = [o for o in day3_available if _pct_change(o.entry_price, o.day3_close) > Decimal("0")]

    best_next = max((_pct_change(o.entry_price, o.next_day_high) for o in next_day_available if o.next_day_high is not None), default=None)
    best_day3 = max((_pct_change(o.entry_price, o.day3_high) for o in day3_available if o.day3_high is not None), default=None)

    lines = ["📘 **主动机会验证**"]
    lines.append(f"- 近{lookback_days}天已记录 {len(outcomes)} 条机会单")
    if next_day_available:
        lines.append(
            f"- 次日收盘红盘 {len(next_day_red)}/{len(next_day_available)}，最佳次日高点 {_fmt_pct(best_next)}"
        )
    if day3_available:
        lines.append(
            f"- 第三日收盘红盘 {len(day3_red)}/{len(day3_available)}，最佳三日高点 {_fmt_pct(best_day3)}"
        )

    for outcome in outcomes[:3]:
        line = (
            f"- {outcome.trade_date.isoformat()} {outcome.name}({outcome.code}) 分{outcome.score}"
            f" | 入场 {outcome.entry_type} @ {outcome.entry_price}"
        )
        if outcome.next_day_close is not None:
            line += f" | 次日收 {_fmt_pct(_pct_change(outcome.entry_price, outcome.next_day_close))}"
        if outcome.day3_close is not None:
            line += f" | 三日收 {_fmt_pct(_pct_change(outcome.entry_price, outcome.day3_close))}"
        lines.append(line)
    return "\n".join(lines)


def evaluate_recent_opportunities(
    conn: sqlite3.Connection,
    data_dir: Path,
    *,
    as_of_date: date,
    lookback_days: int = 7,
) -> list[OpportunityOutcome]:
    journal_path = data_dir / JOURNAL_FILE
    if not journal_path.exists():
        return []

    results: list[OpportunityOutcome] = []
    cutoff = as_of_date - timedelta(days=lookback_days)
    for row in _load_records(journal_path):
        trade_date = date.fromisoformat(row["trade_date"])
        if trade_date < cutoff or trade_date >= as_of_date:
            continue
        entry_price = _decimal(row.get("trigger_price") or row.get("current_price"))
        next_day = _fetch_daily_quote_stats(conn, row["code"], trade_date + timedelta(days=1))
        day3 = _fetch_daily_quote_stats(conn, row["code"], trade_date + timedelta(days=3))
        results.append(
            OpportunityOutcome(
                trade_date=trade_date,
                code=row["code"],
                name=row["name"],
                score=_decimal(row.get("score")),
                entry_type=str(row.get("entry_type", "")),
                entry_price=entry_price,
                next_day_close=next_day["close"] if next_day else None,
                next_day_high=next_day["high"] if next_day else None,
                day3_close=day3["close"] if day3 else None,
                day3_high=day3["high"] if day3 else None,
            )
        )
    results.sort(key=lambda item: (item.trade_date, item.score), reverse=True)
    return results


def _fetch_daily_quote_stats(conn: sqlite3.Connection, code: str, target_date: date) -> dict[str, Decimal] | None:
    rows = conn.execute(
        """
        SELECT current_price, high_price, quote_time
        FROM quotes
        WHERE code = ? AND substr(quote_time, 1, 10) = ?
        ORDER BY quote_time ASC
        """,
        (code, target_date.isoformat()),
    ).fetchall()
    if not rows:
        return None
    close_price = _decimal(rows[-1]["current_price"])
    high_price = max(_decimal(row["high_price"]) for row in rows)
    return {"close": close_price, "high": high_price}


def _load_existing_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    for row in _load_records(path):
        keys.add(f"{row.get('trade_date')}:{row.get('code')}")
    return keys


def _load_records(path: Path) -> list[dict]:
    records: list[dict] = []
    if not path.exists():
        return records
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    except Exception:
        logger.exception("Failed to load opportunity journal: %s", path)
        return []
    return records


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


def _pct_change(base: Decimal, current: Decimal | None) -> Decimal:
    if current is None or base <= 0:
        return Decimal("0")
    return ((current - base) / base * Decimal("100")).quantize(Decimal("0.01"))


def _fmt_pct(value: Decimal | None) -> str:
    if value is None:
        return "暂无"
    return f"{value:+.2f}%"