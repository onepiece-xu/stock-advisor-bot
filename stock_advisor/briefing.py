from __future__ import annotations

from datetime import datetime


def format_mobile_signal(title: str, message: str, *, include_title: bool = True) -> str:
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    selected: list[str] = []
    prefixes = ("时间：", "现价：", "涨跌幅：", "动作：", "评分：", "置信度：", "状态：", "理由：", "风险：")
    for line in lines:
        if line.startswith(prefixes):
            selected.append(line)
        elif line.startswith("观察：") and len(selected) < 10:
            selected.append(line)
        elif line.startswith("动作候选：") and len(selected) < 12:
            selected.append(line)
        if len(selected) >= 12:
            break
    body = "\n".join(selected)
    if include_title:
        return f"{title}\n{body}"
    return body


def format_mobile_digest(items: list[dict]) -> str:
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"【AI股票决策简报】{now_text}"]
    if not items:
        lines.append("暂无已落库的行情决策数据")
        return "\n".join(lines)

    top_items = items[:6]
    for index, item in enumerate(top_items, start=1):
        score = "-" if item["score"] is None else f"{item['score']:.0f}"
        change = _signed(item["change_percent"])
        lines.append(
            f"{index}. {item['code']} {item['name']} | {item['action']} | {score}分 | {change}%"
        )
        lines.append(
            f"   状态:{item['regime']} 置信度:{item['confidence']} 信号:{item['signal_level']}"
        )
        reason = "；".join(item["rationale"][:2]) if item["rationale"] else "暂无明显理由"
        lines.append(f"   理由:{reason}")
        if item["risk_flags"]:
            lines.append(f"   风险:{'；'.join(item['risk_flags'][:2])}")

    hot = [item["code"] for item in items if item["score"] is not None and item["score"] >= 68]
    cold = [item["code"] for item in items if item["score"] is not None and item["score"] < 40]
    if hot:
        lines.append(f"关注偏强: {', '.join(hot[:5])}")
    if cold:
        lines.append(f"注意偏弱: {', '.join(cold[:5])}")
    lines.append("仅供参考，不构成投资建议")
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
