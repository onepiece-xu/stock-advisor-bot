"""Phase 4: Pre-market briefing renderer — pure formatting.

Takes pre-computed data (holdings, triggers, benchmark, plans, etc.)
and returns formatted markdown. Does NOT fetch data or make decisions.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any


def render_pre_market_briefing(
    *,
    today: date,
    benchmark_name: str = "",
    benchmark_price: Decimal = Decimal("0"),
    benchmark_change_pct: Decimal = Decimal("0"),
    yesterday_plan: dict[str, Any] | None = None,
    auction_holdings: list[dict[str, Any]] | None = None,
    active_triggers: list[dict[str, Any]] | None = None,
    announcements: list[str] | None = None,
    account_summary: dict[str, Any] | None = None,
    quick_verdicts: list[str] | None = None,
    exit_plans: list[str] | None = None,
    llm_verdict: str | None = None,
    sector_report: str | None = None,
    opportunities: list[str] | None = None,
    next_session: str = "",
) -> str:
    """Render the full pre-market briefing markdown.

    All data must be pre-fetched by the caller. This function is pure formatting.
    """
    lines: list[str] = [f"📊 {today.strftime('%m/%d')} 盘前"]

    # ── 1. 大盘风向 ──
    if benchmark_name:
        direction = "偏强" if benchmark_change_pct >= 0 else "偏弱"
        lines.append(
            f"\n大盘风向：{benchmark_name} {benchmark_price}（{benchmark_change_pct:+.2f}%）{direction}"
        )

    # ── 2. 昨日计划对照 ──
    if yesterday_plan and yesterday_plan.get("holdings"):
        plan_date = yesterday_plan.get("plan_date", "?")
        lines.append(f"\n【昨日计划对照】{plan_date}")
        for h in yesterday_plan["holdings"]:
            code = h.get("code", "")
            name = h.get("name", "")
            planned_action = h.get("planned_action", "hold")
            yesterday_price = h.get("current_price", 0)
            today_price = h.get("today_price")
            if today_price and yesterday_price > 0:
                overnight_chg = (float(today_price) - yesterday_price) / yesterday_price * 100
                chg_str = f"{overnight_chg:+.2f}%"
            else:
                chg_str = "N/A"
            price_str = f"{today_price}" if today_price else "?"
            action_emoji = {"buy": "🟢", "hold": "🟡", "reduce": "🔴", "avoid": "⛔"}.get(planned_action, "⚪")
            lines.append(
                f"  {action_emoji} {name}({code})："
                f"昨收 {yesterday_price:.2f} | 今竞价 {price_str}（{chg_str}）"
                f" | 昨建议 {planned_action}"
            )
            for t in yesterday_plan.get("triggers", []):
                if t.get("code") == code and not t.get("is_orphan"):
                    t_min = t.get("price_min", 0)
                    t_max = t.get("price_max", 0)
                    if today_price and t_min and t_max:
                        if t_min <= float(today_price) <= t_max:
                            lines.append(
                                f"    ⚡ 昨触发单区间 {t_min}-{t_max}，今竞价 {today_price} 已进入触发区！"
                            )
                        elif abs(float(today_price) - t_min) <= abs(t_min * 0.05):
                            lines.append(
                                f"    👀 昨触发单区间 {t_min}-{t_max}，今竞价 {today_price} 接近触发区"
                            )

    # ── 3. 持仓竞价 ──
    if auction_holdings:
        lines.append("\n【持仓竞价】")
        for h in auction_holdings:
            lines.append(
                f"- {h['name']} {h['price']}（{h['change_pct']:+.2f}%）"
                f" | 盈亏{h['pnl_str']} | {h['stop_label']}{h['stop_price']}"
            )

    # ── 4. 今日触发单 ──
    if active_triggers:
        lines.append("\n【今日触发单】")
        for t in active_triggers:
            lines.append(
                f"- {t['name']}：{t['action']} {t['quantity']}股 "
                f"@ {t['price_min']}-{t['price_max']}"
                f"（回落 {t['fallback_price']}）"
            )

    # ── 5. 近期公告 ──
    if announcements:
        lines.append("\n【近期公告】")
        lines.extend(announcements)

    # ── 6. 账户总览 ──
    if account_summary:
        total = account_summary.get("total_assets", 0)
        cash = account_summary.get("cash", 0)
        cash_pct = account_summary.get("cash_pct", 0)
        lines.append("\n【账户总览】")
        lines.append(f"总资产 {total:.0f} | 现金 {cash:.0f}（{cash_pct:.0f}%）")

        if cash_pct > 60:
            lines.append(f"\n【现金部署评估】现金 {cash:.0f}（{cash_pct:.0f}%）")
            if account_summary.get("can_deploy"):
                lines.append("✅ 大盘环境尚可，现金充裕，可关注今日入场机会")
                lines.append("  首选：现有浮盈持仓加仓 > 已清仓旧标的接回 > 全新标的试仓")
                lines.append("  纪律：单次不超过总资产 10%，涨超 3% 不追，等回踩 MA10")
            else:
                blockers = account_summary.get("deploy_blockers", [])
                lines.append(f"🚫 暂不建议入场：{'；'.join(blockers)}")

    # ── 7. 今日速判 ──
    if quick_verdicts:
        lines.append("\n【今日速判】")
        lines.extend(quick_verdicts)

    # ── 8. 持仓卖点计划 ──
    if exit_plans:
        lines.append("\n【持仓卖点计划】")
        lines.extend(exit_plans)

    # ── 9. AI 决策解读 ──
    if llm_verdict:
        lines.append("\n【AI 决策解读】")
        lines.append(llm_verdict)

    # ── 10. 板块强度 ──
    if sector_report:
        lines.append(f"\n{sector_report}")

    # ── 11. 主动机会 ──
    if opportunities:
        lines.append("\n【今日主动机会】")
        lines.extend(opportunities)

    if next_session:
        lines.append(f"\n下次开盘：{next_session}")

    return "\n".join(lines)
