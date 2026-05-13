"""LLM-powered stock analysis — generates concise decision interpretations.

Uses DeepSeek API + strategy YAML files to convert structured data into
2-3 sentence human-readable verdicts. Strategy files in strategies/ provide
trading discipline context matched to each holding's state.

Design: fire-and-forget with timeout. API failure → section skipped, daemon continues.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import requests
import yaml

logger = logging.getLogger(__name__)

DEEPSEEK_API_KEY = "sk-6071217d15f44505b3db5f13d635ce42"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"
REQUEST_TIMEOUT = 15

STRATEGIES_DIR = Path(__file__).parent.parent / "strategies"


# ── Strategy loading ──

_cached_strategies: dict[str, str] | None = None


def _load_strategies() -> dict[str, str]:
    global _cached_strategies
    if _cached_strategies is not None:
        return _cached_strategies
    strategies: dict[str, str] = {}
    if STRATEGIES_DIR.exists():
        for f in sorted(STRATEGIES_DIR.glob("*.yaml")):
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
                if data and "instructions" in data:
                    strategies[data.get("name", f.stem)] = data["instructions"]
            except Exception:
                logger.warning("Failed to load strategy: %s", f)
    _cached_strategies = strategies
    return strategies


def _match_strategies(holdings: list[dict]) -> str:
    """Match strategies to holdings based on position state."""
    strategies = _load_strategies()
    matched: set[str] = set()

    for h in holdings:
        pnl = h.get("pnl_pct", 0)
        if pnl <= -20:
            matched.add("deep_loss_exit")
        elif pnl >= 5:
            matched.add("bull_trend_hold")

    cash_pct = holdings[0].get("_cash_pct", 0) if holdings else 0
    if cash_pct > 60:
        matched.add("cash_deploy")

    lines: list[str] = []
    for name in matched:
        if name in strategies:
            lines.append(f"【{name}】\n{strategies[name]}")
    return "\n\n".join(lines)


# ── Public API ──

def generate_briefing_verdict(
    holdings_data: list[dict],
    market_wind: str = "",
    cash_pct: float = 0,
    today: str = "",
) -> str:
    if not holdings_data:
        return ""
    prompt = _build_morning_prompt(holdings_data, market_wind, cash_pct, today)
    return _call_deepseek(prompt, max_tokens=300)


def generate_close_verdict(
    holdings_data: list[dict],
    avg_score: float = 50,
    market_summary: str = "",
    today: str = "",
) -> str:
    if not holdings_data:
        return ""
    prompt = _build_close_prompt(holdings_data, avg_score, market_summary, today)
    return _call_deepseek(prompt, max_tokens=400)


# ── Prompt builders ──

def _build_morning_prompt(
    holdings: list[dict],
    market_wind: str,
    cash_pct: float,
    today: str,
) -> str:
    # Tag holdings with cash_pct for strategy matching
    for h in holdings:
        h["_cash_pct"] = cash_pct
    strategy_context = _match_strategies(holdings)

    lines = [
        f"你是A股交易顾问。用户是上班族，只看这一条消息做决策。今天是{today}开盘前。",
        "用2-3句话给出今日操作建议。语气直接，不客套。",
    ]
    if strategy_context:
        lines.append("")
        lines.append("## 适用的交易纪律（必须遵守）")
        lines.append(strategy_context)
    lines.extend([
        "",
        f"## 当前数据",
        f"大盘：{market_wind}",
        f"现金占比：{cash_pct:.0f}%",
        "",
        "持仓：",
    ])
    for h in holdings:
        h.pop("_cash_pct", None)
        trigger_info = h.get("trigger_note", "")
        lines.append(
            f"- {h['name']}({h['code']})：{h['quantity']}股，成本{h['cost_price']}，"
            f"现价{h['current_price']}，盈亏{h['pnl_pct']:+.1f}%，止损{h['stop_price']}"
            f"{'，' + trigger_info if trigger_info else ''}"
        )
    lines.extend([
        "",
        "请输出：",
        "1. 今日总体建议（一句话）",
        "2. 每只票的操作要点（每条≤15字，严格遵守上方交易纪律）",
    ])
    return "\n".join(lines)


def _build_close_prompt(
    holdings: list[dict],
    avg_score: float,
    market_summary: str,
    today: str,
) -> str:
    for h in holdings:
        h["_cash_pct"] = 0  # close review doesn't have cash context
    strategy_context = _match_strategies(holdings)

    lines = [
        f"你是A股交易顾问。今天是{today}收盘后。用2-3句话给出明日操作建议。语气直接。",
    ]
    if strategy_context:
        lines.append("")
        lines.append("## 适用的交易纪律（必须遵守）")
        lines.append(strategy_context)
    lines.extend([
        "",
        f"## 今日数据",
        f"平均评分：{avg_score:.0f}/100",
        f"市场概况：{market_summary}",
        "",
        "持仓：",
    ])
    for h in holdings:
        h.pop("_cash_pct", None)
        lines.append(
            f"- {h['name']}({h['code']})：{h['quantity']}股，现价{h['current_price']}，"
            f"盈亏{h['pnl_pct']:+.1f}%，建议动作{h.get('action', 'hold')}，评分{h.get('score', '-')}"
        )
    lines.extend([
        "",
        "输出2-3句话：明天重点看什么，要不要动，如果动先动哪只。严格遵守上方交易纪律。",
    ])
    return "\n".join(lines)


# ── API call ──

def _call_deepseek(prompt: str, max_tokens: int = 300) -> str:
    try:
        resp = requests.post(
            f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": "你是专业A股交易顾问。严格遵守用户提供的交易纪律。回答直接、简洁、可操作。"},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("DeepSeek API error status=%s", resp.status_code)
            return ""
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning("DeepSeek API call failed: %s", exc)
        return ""
