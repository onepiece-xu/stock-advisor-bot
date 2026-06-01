"""
辩论→触发单同步 — Debate-to-Trigger Sync

核心理念：
  收盘多 Agent 辩论的结论可以反哺第二天的卖出计划，
  但不能把“建议卖出”直接落成“次日开盘全仓硬砍”。

当前规则：
  - 只生成“辩论减仓”触发单，不生成全仓强平单
  - 最多减 1/3 仓位，且至少保留一手观察仓
  - 不覆盖既有的 exit_plan/profit 触发单，只替换旧的 debate_sync 触发单

使用方式：
  在 review.py 的 build_close_review() 末尾调用 sync_debate_to_triggers()
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Minimum confidence for debate decision to override existing triggers
MIN_CONFIDENCE_OVERRIDE = 0.65
# How many recent debate rounds to consider per stock
MAX_DEBATE_ROUNDS = 3


def _debate_reduce_quantity(total_qty: int) -> int:
    """Return a conservative sell size for debate-based exits."""
    if total_qty <= 0:
        return 0
    if total_qty <= 100:
        return 0

    raw_qty = max(100, (total_qty // 3 // 100) * 100)
    max_reducible = max(0, total_qty - 100)
    return min(raw_qty, max_reducible)


def sync_debate_to_triggers(data_dir: Path, *, dry_run: bool = False) -> list[str]:
    """Read latest debate decisions and sync to trading_plan.json.

    Returns list of action descriptions for logging/reporting.
    """
    debate_path = data_dir / "feedback" / "debate_log.jsonl"
    plan_path = data_dir / "trading_plan.json"

    if not debate_path.exists():
        logger.info("No debate log found, skipping trigger sync")
        return []

    if not plan_path.exists():
        logger.info("No trading_plan.json found, skipping trigger sync")
        return []

    # Load debate log
    debates: list[dict] = []
    try:
        with open(debate_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    debates.append(json.loads(line))
    except Exception as exc:
        logger.warning("Failed to read debate log: %s", exc)
        return []

    if not debates:
        return []

    # Load trading plan
    try:
        with open(plan_path) as f:
            plan = json.load(f)
    except Exception as exc:
        logger.warning("Failed to read trading_plan.json: %s", exc)
        return []

    # Group debates by symbol, keep most recent
    symbol_debates: dict[str, dict] = {}
    for d in debates:
        symbol = d.get("symbol", "")
        ts = d.get("timestamp", "")
        if symbol and ts:
            if symbol not in symbol_debates or ts > symbol_debates[symbol].get("timestamp", ""):
                symbol_debates[symbol] = d

    actions_taken: list[str] = []
    triggers = plan.get("triggers", [])

    for symbol, debate in symbol_debates.items():
        action = debate.get("action", "hold")
        confidence = debate.get("confidence", 0)
        name = debate.get("name", symbol)
        price = debate.get("price", 0)
        vote_summary = debate.get("vote_summary", "")
        reasoning = debate.get("reasoning", "")

        # Only act on high-confidence sell decisions
        if action != "sell" or confidence < MIN_CONFIDENCE_OVERRIDE:
            continue

        # Check if 风控 is among the sellers
        agent_votes = debate.get("agent_votes", {})
        risk_control_selling = agent_votes.get("铁血风控") == "sell"

        # Count sell votes
        sell_count = sum(1 for v in agent_votes.values() if v == "sell")
        total_agents = len(agent_votes)

        if sell_count < 3 and not risk_control_selling:
            continue  # Not enough consensus

        # Parse stock code from symbol
        code = str(symbol.replace("sh", "").replace("sz", ""))
        
        # Build a conservative debate-based reduce trigger.
        # Debate output is advisory, so it must not become a next-day full liquidation.
        stop_price = float(price)
        stop_price_min = round(stop_price - 0.10, 2)
        stop_price_max = round(stop_price + 0.10, 2)
        fallback = round(stop_price - 0.05, 2)

        # Get actual quantity from portfolio snapshot
        qty = 0
        try:
            snapshot_path = data_dir.parent / "portfolio-snapshot.json"
            if snapshot_path.exists():
                snap = json.loads(snapshot_path.read_text())
                for h in snap.get("holdings", []):
                    if h.get("code", "") == code:
                        qty = int(h.get("quantity", 0))
                        break
        except Exception:
            pass

        if qty <= 0:
            continue

        reduce_qty = _debate_reduce_quantity(qty)
        if reduce_qty <= 0:
            logger.info("Debate sync: skip %s because quantity=%s leaves no safe reduce lot", code, qty)
            continue

        new_trigger: dict[str, Any] = {
            "code": symbol.replace("sh", "").replace("sz", ""),
            "name": f"{name}-辩论减仓",
            "action": "sell",
            "quantity": reduce_qty,
            "priceMin": str(stop_price_min),
            "priceMax": str(stop_price_max),
            "fallbackPrice": str(fallback),
            "note": f"收盘辩论{sell_count}/{total_agents}建议先减仓，不做全仓硬砍。风控{'已' if risk_control_selling else '未'}触发。{reasoning[:80]}",
            "disableBuy": True,
            "state": "armed",
            "_source": "debate_sync",
            "_created": datetime.now().isoformat(),
        }

        # Only replace prior debate-generated triggers for this stock.
        # Preserve exit-plan / profit-plan triggers.
        old_triggers = [
            t for t in triggers
            if str(t.get("code", "")) == code and t.get("_source") == "debate_sync"
        ]
        if old_triggers:
            old_names = [t.get("name", "?") for t in old_triggers]
            triggers = [
                t for t in triggers
                if not (str(t.get("code", "")) == code and t.get("_source") == "debate_sync")
            ]
            actions_taken.append(
                f"🗑 {name}: 移除旧辩论触发单 {old_names} → 替换为辩论减仓 {reduce_qty}股 @{stop_price}"
            )
        else:
            actions_taken.append(
                f"➕ {name}: 新增辩论减仓 {reduce_qty}股 @{stop_price}（{sell_count}/{total_agents}建议卖出）"
            )

        triggers.append(new_trigger)

    if not actions_taken:
        logger.info("Debate sync: no changes needed")
        return []

    # Commit
    plan["triggers"] = triggers
    plan["_debate_synced_at"] = datetime.now().isoformat()

    if not dry_run:
        # Backup old plan
        backup_path = plan_path.with_suffix(".json.bak")
        try:
            import shutil
            shutil.copy2(plan_path, backup_path)
        except Exception:
            pass

        with open(plan_path, "w") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
        logger.info("Debate sync: updated trading_plan.json with %d changes", len(actions_taken))
    else:
        logger.info("Debate sync (dry_run): would apply %d changes", len(actions_taken))

    return actions_taken
