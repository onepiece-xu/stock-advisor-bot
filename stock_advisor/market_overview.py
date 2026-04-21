from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .providers import EastmoneyMarketSnapshotProvider


@dataclass(slots=True)
class MarketOverview:
    generated_at: str
    up_count: int | None
    flat_count: int | None
    down_count: int | None
    top_gainers: list[dict]
    top_losers: list[dict]
    top_industries: list[dict]
    top_concepts: list[dict]
    warnings: list[str]


def build_market_overview(config, *, top_n: int = 5) -> MarketOverview:
    provider = EastmoneyMarketSnapshotProvider(config.monitor)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    up_count: int | None = None
    flat_count: int | None = None
    down_count: int | None = None
    warnings: list[str] = []
    try:
        breadth = provider.fetch_market_breadth()
        generated_at = breadth["generated_at"]
        up_count = breadth["up_count"]
        flat_count = breadth["flat_count"]
        down_count = breadth["down_count"]
    except Exception:
        warnings.append("涨跌家数接口波动，已降级显示其余市场扫描结果")

    try:
        top_gainers = provider.fetch_top_stocks(limit=top_n, descending=True)
    except Exception:
        top_gainers = []
        warnings.append("领涨个股暂不可用")

    try:
        top_losers = provider.fetch_top_stocks(limit=top_n, descending=False)
    except Exception:
        top_losers = []
        warnings.append("领跌个股暂不可用")

    try:
        top_industries = provider.fetch_sector_boards(kind="industry", limit=top_n)
    except Exception:
        top_industries = []
        warnings.append("热点行业暂不可用")

    try:
        top_concepts = provider.fetch_sector_boards(kind="concept", limit=top_n)
    except Exception:
        top_concepts = []
        warnings.append("热点概念暂不可用")

    overview = MarketOverview(
        generated_at=generated_at,
        up_count=up_count,
        flat_count=flat_count,
        down_count=down_count,
        top_gainers=top_gainers,
        top_losers=top_losers,
        top_industries=top_industries,
        top_concepts=top_concepts,
        warnings=warnings,
    )
    if _has_live_data(overview):
        _save_market_overview_cache(config, overview)
        return overview

    cached = _load_market_overview_cache(config)
    if cached is not None:
        cached.warnings.insert(0, "实时接口波动，以下为最近一次成功市场快照")
        return cached
    return overview


def render_market_overview(overview: MarketOverview, *, mobile: bool = False) -> str:
    lines = [f"【市场概览】{overview.generated_at}"]
    if overview.up_count is None or overview.flat_count is None or overview.down_count is None:
        lines.append("全市场: 涨跌家数暂不可用")
    else:
        lines.append(f"全市场: 上涨 {overview.up_count} | 平盘 {overview.flat_count} | 下跌 {overview.down_count}")
    lines.append("")
    lines.append("热点行业:")
    lines.extend(_render_boards(overview.top_industries, mobile=mobile))
    lines.append("")
    lines.append("热点概念:")
    lines.extend(_render_boards(overview.top_concepts, mobile=mobile))
    lines.append("")
    lines.append("领涨个股:")
    lines.extend(_render_stocks(overview.top_gainers, mobile=mobile))
    if not mobile:
        lines.append("")
        lines.append("领跌个股:")
        lines.extend(_render_stocks(overview.top_losers, mobile=mobile))
    if overview.warnings:
        lines.append("")
        lines.append(f"提示: {overview.warnings[0]}")
    lines.append("仅供参考，不构成投资建议")
    return "\n".join(lines)


def _render_boards(items: list[dict], *, mobile: bool) -> list[str]:
    if not items:
        return ["- 暂无数据"]
    lines: list[str] = []
    limit = 4 if mobile else len(items)
    for item in items[:limit]:
        lines.append(
            f"- {item['name']} {_signed(item['change_percent'])}% | 涨:{item['up_count']} 跌:{item['down_count']} | 龙头 {item['leader_name']} {_signed(item['leader_change_percent'])}%"
        )
    return lines


def _render_stocks(items: list[dict], *, mobile: bool) -> list[str]:
    if not items:
        return ["- 暂无数据"]
    lines: list[str] = []
    limit = 4 if mobile else len(items)
    for item in items[:limit]:
        sector_text = item.get("industry_name") or item.get("concept_name") or "未知板块"
        lines.append(
            f"- {item['code']} {item['name']} {_signed(item['change_percent'])}% | 成交额 {item['turnover_yi']:.2f}亿 | {sector_text}"
        )
    return lines


def _signed(value: float | None) -> str:
    if value is None:
        return "-"
    return f"+{value:.2f}" if value > 0 else f"{value:.2f}"


def _has_live_data(overview: MarketOverview) -> bool:
    return any(
        [
            overview.up_count is not None,
            overview.flat_count is not None,
            overview.down_count is not None,
            bool(overview.top_gainers),
            bool(overview.top_losers),
            bool(overview.top_industries),
            bool(overview.top_concepts),
        ]
    )


def _market_overview_cache_path(config) -> Path | None:
    storage = getattr(config, "storage", None)
    sqlite_path = getattr(storage, "sqlite_path", None)
    if not sqlite_path:
        return None
    return Path(sqlite_path).resolve().parent / "market_overview_cache.json"


def _save_market_overview_cache(config, overview: MarketOverview) -> None:
    cache_path = _market_overview_cache_path(config)
    if cache_path is None:
        return
    cache_path.write_text(
        json.dumps(asdict(overview), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_market_overview_cache(config) -> MarketOverview | None:
    cache_path = _market_overview_cache_path(config)
    if cache_path is None or not cache_path.exists():
        return None
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    return MarketOverview(
        generated_at=str(payload.get("generated_at") or datetime.now().strftime("%Y-%m-%d %H:%M")),
        up_count=payload.get("up_count"),
        flat_count=payload.get("flat_count"),
        down_count=payload.get("down_count"),
        top_gainers=list(payload.get("top_gainers") or []),
        top_losers=list(payload.get("top_losers") or []),
        top_industries=list(payload.get("top_industries") or []),
        top_concepts=list(payload.get("top_concepts") or []),
        warnings=list(payload.get("warnings") or []),
    )
