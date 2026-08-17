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

# DeepSeek config — loaded from config.yaml (secure) with fallback chain
# Priority: config.yaml > env vars > hardcoded defaults (for tests only)
_DEEPSEEK_CONFIG = None


def _load_deepseek_config():
    """Lazy-load DeepSeek config from the bot configuration."""
    global _DEEPSEEK_CONFIG
    if _DEEPSEEK_CONFIG is not None:
        return _DEEPSEEK_CONFIG
    try:
        from .config import load_config
        from pathlib import Path
        config_path = Path(__file__).parent.parent / "config.yaml"
        if config_path.exists():
            cfg = load_config(str(config_path))
            if cfg.deepseek and cfg.deepseek.api_key:
                _DEEPSEEK_CONFIG = cfg.deepseek
                return _DEEPSEEK_CONFIG
    except Exception as exc:
        logger.warning("stock_advisor/llm_analyst.py:_load_deepseek_config failed: %s", exc)
    # Fallback: env vars (for container/CI)
    import os
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if api_key:
        from .config import DeepSeekConfig
        _DEEPSEEK_CONFIG = DeepSeekConfig(
            api_key=api_key,
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro"),
        )
        return _DEEPSEEK_CONFIG
    # Last resort (for tests without config file)
    from .config import DeepSeekConfig
    _DEEPSEEK_CONFIG = DeepSeekConfig(api_key="", base_url="https://api.deepseek.com", model="deepseek-v4-pro")
    return _DEEPSEEK_CONFIG


def get_deepseek_api_key() -> str:
    return _load_deepseek_config().api_key


def get_deepseek_base_url() -> str:
    return _load_deepseek_config().base_url


def get_deepseek_model() -> str:
    return _load_deepseek_config().model


# Legacy module-level constants (for backward compat with other modules that import these)
# These are now lazy-loaded from config at import time
DEEPSEEK_API_KEY = property(lambda self: get_deepseek_api_key()) if False else ""
DEEPSEEK_BASE_URL = "https://api.deepseek.com"  # default, overridden at call time
DEEPSEEK_MODEL = "deepseek-v4-pro"  # default, overridden at call time
REQUEST_TIMEOUT = 25  # pro model needs more time

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
            except Exception as exc:
                logger.warning("stock_advisor/llm_analyst.py:_load_strategies failed: %s", exc)
                logger.warning("Failed to load strategy: %s", f)
    _cached_strategies = strategies
    return strategies


def _match_strategies(holdings: list[dict]) -> str:
    """Match strategies to holdings based on position state.

    Strategy-to-condition mapping:
      emotion_cycle  — always (meta-strategy / thinking framework)
      deep_loss_exit — pnl <= -20%
      bottom_volume  — pnl <= -30% (deepest loss, bottom detection)
      bull_trend_hold — pnl >= 5%
      volume_breakout — pnl >= 5% or cash > 60%
      shrink_pullback — cash > 60% (entry timing)
      cash_deploy     — cash > 60%
    """
    strategies = _load_strategies()
    matched: set[str] = set()

    # Always include emotion cycle as the thinking framework
    matched.add("emotion_cycle")

    has_deep_loss = False
    has_winner = False

    for h in holdings:
        pnl = h.get("pnl_pct", 0)
        if pnl <= -30:
            matched.add("deep_loss_exit")
            matched.add("bottom_volume")
            has_deep_loss = True
        elif pnl <= -20:
            matched.add("deep_loss_exit")
            has_deep_loss = True
        elif pnl >= 5:
            matched.add("bull_trend_hold")
            has_winner = True

    cash_pct = holdings[0].get("_cash_pct", 0) if holdings else 0
    if cash_pct > 60:
        matched.add("cash_deploy")
        matched.add("shrink_pullback")

    # volume_breakout useful when we have winners near resistance or cash to deploy
    if has_winner or cash_pct > 60:
        matched.add("volume_breakout")

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
        "2. 买什么：若今天不该新开仓，就明确写不买新票",
        "3. 什么时候买：给出具体触发条件或等待条件",
        "4. 什么时候卖：对每只持仓给出止损/卖点/移动止盈思路（每条≤18字）",
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
        "输出2-3句话，必须回答三件事：1) 明天买什么；2) 什么时候买；3) 什么时候卖。",
        "如果不该开新仓，就明确说不买；如果要卖，优先写止损/卖点，不要写等回本。",
    ])
    return "\n".join(lines)


# ── API call ──

def _call_deepseek(prompt: str, max_tokens: int = 300) -> str:
    try:
        resp = requests.post(
            f"{get_deepseek_base_url()}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {get_deepseek_api_key()}",
                "Content-Type": "application/json",
            },
            json={
                "model": get_deepseek_model(),
                "messages": [
                    {"role": "system", "content": "你是专业A股交易顾问。严格遵守用户提供的交易纪律。回答直接、简洁、可操作。\n\n【A股交易铁律 — 违反等于废单】\n- 最小交易单位：100股（1手），挂单量必须是100的整数倍（100/200/300/500/1000...）\n- 50股、150股、250股等不是100整数倍的挂单无法成交，绝对禁止\n- 建议数量时只说100的整数倍，如\"减仓200股\"\"买入100股\"\"加仓300股\"\n- 如果不知道该卖多少，宁可说\"减仓\"不写数量，也不要写50股"},
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
