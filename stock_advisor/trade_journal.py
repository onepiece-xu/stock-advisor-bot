"""
交易复盘日志 — 记录每笔买卖的原因和事后验证结果

数据存储: data/trade_journal/trades.jsonl
每行一条JSON记录，包含买入原因、卖出结果、事后验证。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
import logging
from datetime import date
logger = logging.getLogger(__name__)
from time import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .market_hours import MARKET_TZ


@dataclass
class TradeEntry:
    """一笔交易的完整记录"""
    entry_id: str
    symbol: str  # e.g. sh601698
    name: str
    side: str  # buy / sell
    quantity: int
    price: float
    amount: float  # quantity * price
    reason: str  # 买入/卖出原因
    strategy: str  # 使用的策略名，如 cash_deploy / deep_loss_exit / take_profit
    confidence: str  # high / medium / low
    market_context: str  # 市场环境简述
    created_at: str  # ISO datetime
    trade_date: str  # ISO date

    # 事后验证字段（sell时填写）
    buy_price: float | None = None  # 对应买入价
    buy_date: str | None = None  # 对应买入日期
    holding_days: int | None = None
    pnl_pct: float | None = None  # 盈亏百分比
    verdict: str | None = None  # 事后评价：good / bad / neutral
    lessons: str | None = None  # 经验教训


class TradeJournal:
    """交易日志管理器"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.data_dir / "trades.jsonl"

    def log_buy(
        self,
        symbol: str,
        name: str,
        quantity: int,
        price: Decimal,
        reason: str,
        *,
        strategy: str = "cash_deploy",
        confidence: str = "medium",
        market_context: str = "",
    ) -> TradeEntry:
        """记录一笔买入"""
        now = datetime.now(MARKET_TZ)
        entry = TradeEntry(
            entry_id=f"buy_{now.strftime('%Y%m%d_%H%M%S')}_{symbol}",
            symbol=symbol,
            name=name,
            side="buy",
            quantity=quantity,
            price=float(price),
            amount=float(price * quantity),
            reason=reason,
            strategy=strategy,
            confidence=confidence,
            market_context=market_context,
            created_at=now.isoformat(),
            trade_date=now.date().isoformat(),
        )
        self._append(entry)
        return entry

    def log_sell(
        self,
        symbol: str,
        name: str,
        quantity: int,
        price: Decimal,
        reason: str,
        *,
        buy_price: float | None = None,
        buy_date: str | None = None,
        strategy: str = "deep_loss_exit",
        confidence: str = "medium",
        market_context: str = "",
    ) -> TradeEntry:
        """记录一笔卖出"""
        now = datetime.now(MARKET_TZ)
        pnl_pct = None
        holding_days = None
        if buy_price and buy_price > 0:
            pnl_pct = round((float(price) - buy_price) / buy_price * 100, 2)
        if buy_date:
            try:
                buy_dt = date.fromisoformat(buy_date)
                holding_days = (now.date() - buy_dt).days
            except ValueError:
                pass

        entry = TradeEntry(
            entry_id=f"sell_{now.strftime('%Y%m%d_%H%M%S')}_{symbol}",
            symbol=symbol,
            name=name,
            side="sell",
            quantity=quantity,
            price=float(price),
            amount=float(price * quantity),
            reason=reason,
            strategy=strategy,
            confidence=confidence,
            market_context=market_context,
            created_at=now.isoformat(),
            trade_date=now.date().isoformat(),
            buy_price=buy_price,
            buy_date=buy_date,
            holding_days=holding_days,
            pnl_pct=pnl_pct,
        )
        self._append(entry)
        return entry

    def verify_trade(
        self,
        entry_id: str,
        verdict: str,
        lessons: str,
    ) -> bool:
        """事后验证一笔交易：good / bad / neutral"""
        entries = self._read_all()
        updated = False
        new_entries = []
        for e in entries:
            if e.get("entry_id") == entry_id:
                e["verdict"] = verdict
                e["lessons"] = lessons
                updated = True
            new_entries.append(e)

        if updated:
            self.journal_path.write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in new_entries) + "\n"
            )
        return updated

    def get_stats(self) -> dict:
        """获取交易统计"""
        entries = self._read_all()
        buys = [e for e in entries if e["side"] == "buy"]
        sells = [e for e in entries if e["side"] == "sell"]

        verified_sells = [e for e in sells if e.get("verdict")]
        good = sum(1 for e in verified_sells if e["verdict"] == "good")
        bad = sum(1 for e in verified_sells if e["verdict"] == "bad")

        total_pnl = sum(e["pnl_pct"] for e in sells if e.get("pnl_pct"))

        return {
            "total_trades": len(entries),
            "total_buys": len(buys),
            "total_sells": len(sells),
            "verified_trades": len(verified_sells),
            "good_trades": good,
            "bad_trades": bad,
            "win_rate": round(good / len(verified_sells) * 100, 1) if verified_sells else 0,
            "total_pnl_pct": round(total_pnl, 2),
            "avg_holding_days": round(
                sum(e["holding_days"] for e in sells if e.get("holding_days")) / max(1, len([e for e in sells if e.get("holding_days")])),
                1,
            ),
        }

    def get_strategy_stats(self) -> dict:
        """按策略统计交易表现。

        Returns:
            {strategy_name: {total, buys, sells, verified, good, bad, win_rate, total_pnl, avg_holding_days}}
        """
        entries = self._read_all()
        sells = [e for e in entries if e["side"] == "sell"]

        # Group by strategy
        strategies: dict[str, list[dict]] = {}
        for e in sells:
            s = e.get("strategy", "unknown")
            strategies.setdefault(s, []).append(e)

        result = {}
        for strategy, trades in sorted(strategies.items()):
            verified = [t for t in trades if t.get("verdict")]
            good = sum(1 for t in verified if t["verdict"] == "good")
            bad = sum(1 for t in verified if t["verdict"] == "bad")
            total_pnl = sum(t["pnl_pct"] for t in trades if t.get("pnl_pct"))
            holding_days = [t["holding_days"] for t in trades if t.get("holding_days")]

            result[strategy] = {
                "total": len(trades),
                "verified": len(verified),
                "good": good,
                "bad": bad,
                "win_rate": round(good / len(verified) * 100, 1) if verified else 0,
                "total_pnl_pct": round(total_pnl, 2),
                "avg_holding_days": round(sum(holding_days) / len(holding_days), 1) if holding_days else 0,
            }
        return result

    def auto_verify(self) -> int:
        """Auto-verify all unverified sells by matching with buys.

        For each unverified sell entry, finds the most recent buy for the same
        symbol that hasn't been matched yet, computes actual PnL, and assigns
        a verdict based on PnL threshold:
          - PnL > 0 → good
          - PnL < -5% → bad
          - else → neutral

        Returns count of verified trades.
        """
        entries = self._read_all()
        buys = [e for e in entries if e["side"] == "buy"]
        sells = [e for e in entries if e["side"] == "sell"]

        verified = 0
        used_buy_ids: set[str] = set()

        for sell in sells:
            if sell.get("verdict"):
                continue  # Already verified

            # Find matching buy: same symbol, before sell date, not already used
            sell_date = sell.get("trade_date", "")
            symbol = sell.get("symbol", "")
            sell_qty = sell.get("quantity", 0)

            matching_buys = [
                b for b in buys
                if b["symbol"] == symbol
                and b.get("trade_date", "") <= sell_date
                and b["entry_id"] not in used_buy_ids
            ]

            if not matching_buys:
                continue

            # Use the most recent buy before this sell
            buy = matching_buys[-1]
            buy_price = buy.get("price", 0)
            sell_price = sell.get("price", 0)

            if buy_price <= 0:
                continue

            pnl_pct = round((sell_price - buy_price) / buy_price * 100, 2)
            buy_date = buy.get("trade_date", "")
            holding_days = 0
            try:
                from datetime import date
                holding_days = (date.fromisoformat(sell_date) - date.fromisoformat(buy_date)).days
            except Exception as exc:
                logger.warning("Failed to compute holding_days: %s", exc)

            # Auto-verdict
            if pnl_pct > 0:
                verdict = "good"
            elif pnl_pct < -5:
                verdict = "bad"
            else:
                verdict = "neutral"

            sell["pnl_pct"] = pnl_pct
            sell["buy_price"] = buy_price
            sell["buy_date"] = buy_date
            sell["holding_days"] = holding_days
            sell["verdict"] = verdict
            if not sell.get("lessons"):
                sell["lessons"] = f"自动验证：策略={sell.get('strategy','unknown')}，PnL={pnl_pct:+.1f}%"

            used_buy_ids.add(buy["entry_id"])
            verified += 1

        if verified > 0:
            self.journal_path.write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n"
            )

        return verified

    def _append(self, entry: TradeEntry) -> None:
        with open(self.journal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

    def _read_all(self) -> list[dict]:
        if not self.journal_path.exists():
            return []
        entries = []
        with open(self.journal_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        return entries