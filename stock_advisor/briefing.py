from __future__ import annotations

from datetime import datetime


def format_mobile_signal(title: str, message: str, *, include_title: bool = True) -> str:
    lines = [line.strip() for line in message.splitlines() if line.strip()]

    def pick(*prefixes: str) -> str | None:
        for prefix in prefixes:
            for line in lines:
                if line.startswith(prefix):
                    return line
        return None

    brief_lines: list[str] = []
    if include_title:
        brief_lines.append(title)

    for line in (
        pick("操作指令：", "直接建议：", "动作："),
        pick("执行数量："),
        pick("当前持仓："),
        pick("触发条件："),
        pick("风险："),
    ):
        if line:
            brief_lines.append(line)

    if len(brief_lines) <= (1 if include_title else 0):
        fallback = lines[:3]
        if include_title:
            brief_lines.extend(fallback)
        else:
            brief_lines = fallback

    return "\n".join(brief_lines[:5])


def format_mobile_digest(items: list[dict], positions: dict[str, int] | None = None) -> str:
    now_text = datetime.now().strftime("%H:%M")
    lines = [f"📊 {now_text}"]
    if not items:
        return f"📊 {now_text}\n暂无信号"

    top_items = items[:6]
    for item in top_items:
        change = _signed(item["change_percent"])
        action_icon = {"buy": "🟢", "hold": "🟡", "reduce": "🔴", "avoid": "⛔"}.get(item["action"], "")
        qty = item.get("current_position_qty", 0)
        if qty <= 0 and positions:
            qty = positions.get(item.get("code", ""), 0)
        qty_str = f"[{qty}股]" if qty > 0 else ""
        # One line: icon name action change [qty]
        lines.append(f"{action_icon} {item['name']} {item['action']} {change}% {qty_str}")
        # Show concrete trade instruction
        advice = item.get("trade_advice", "")
        if advice and advice not in ("持有", "空仓观望", "空仓等待"):
            lines.append(f"  → {advice}")
        # Critical risks only
        risks = item.get("risk_flags", [])
        critical = [r for r in risks if any(kw in r for kw in ("⚠️", "🎯", "止损", "除权", "暴跌"))]
        if critical:
            lines.append(f"  ⚠️ {'; '.join(critical[:2])}")

    lines.append("—" * 10)
    return "\n".join(lines)


def format_mobile_replay(stats: dict, *, symbol: str | None = None, level: str | None = None, action: str | None = None) -> str:
    lines = ["【历史回放统计】"]
    filters = [part for part in (symbol, level, action) if part]
    if filters:
        lines.append(f"过滤: {' / '.join(filters)}")
    lines.append(f"样本数: {stats['signal_count']}")
    if stats.get("avg_score") is not None:
        lines.append(f"平均分: {stats['avg_score']:.2f}")
    breakdown = stats.get("action_breakdown") or {}
    if breakdown:
        lines.append("动作分布: " + " | ".join(f"{k}:{v}" for k, v in breakdown.items()))
    for horizon, summary in stats["horizons"].items():
        lines.append(
            f"{horizon}周期后 -> 样本{summary['samples']} 平均{_pct(summary['avg'])} 中位{_pct(summary['median'])} 胜率{_pct(summary['win_rate'])}"
        )
    lines.append("仅供参考，不构成投资建议")
    return "\n".join(lines)


def _signed(value: float | None) -> str:
    if value is None:
        return "-"
    return f"+{value:.2f}" if value > 0 else f"{value:.2f}"


def _pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}%"
