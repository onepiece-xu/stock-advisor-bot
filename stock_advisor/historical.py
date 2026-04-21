from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from .analysis import analyze_quotes
from .briefing import format_mobile_signal
from .config import AppConfig
from .habit_learning import build_trading_habit_profile
from .models import ObservationResult, StockQuote, StockRef
from .portfolio import find_holding, load_snapshot as load_portfolio_snapshot
from .providers import EastmoneyMinuteHistoryProvider
from .storage import cache_quotes, connect_db, load_recent_quotes_before

from decimal import Decimal as _Decimal


def _compute_cash_ratio(snapshot) -> _Decimal | None:
    if snapshot is None or snapshot.total_assets <= 0:
        return None
    return (snapshot.cash / snapshot.total_assets).quantize(_Decimal("0.0001"))


@dataclass(slots=True)
class HistoricalAdviceItem:
    stock: StockRef
    requested_at: datetime
    matched_at: datetime
    quote: StockQuote
    result: ObservationResult
    sample_size: int

    @property
    def exact_match(self) -> bool:
        return self.requested_at == self.matched_at


@dataclass(slots=True)
class HistoricalComparisonItem:
    stock: StockRef
    start: HistoricalAdviceItem
    end: HistoricalAdviceItem


def analyze_historical_point(
    config: AppConfig,
    requested_at: datetime,
    *,
    stocks: list[StockRef] | None = None,
) -> list[HistoricalAdviceItem]:
    selected_stocks = stocks or config.monitor.stocks
    conn = connect_db(config.storage.sqlite_path)
    provider = EastmoneyMinuteHistoryProvider(config.monitor)
    portfolio_snapshot = _load_portfolio_snapshot(config)
    cash_ratio = _compute_cash_ratio(portfolio_snapshot)
    benchmark_history = _load_benchmark_history(config, provider, requested_at)
    trading_habit_profile = build_trading_habit_profile(conn)
    items: list[HistoricalAdviceItem] = []

    for stock in selected_stocks:
        history = _load_history_for_point(
            conn,
            provider,
            stock,
            requested_at,
            config.monitor.history_size,
        )
        if not history:
            raise RuntimeError(f"{stock.code} 在 {requested_at:%Y-%m-%d %H:%M:%S} 前没有可用分钟行情")
        result = analyze_quotes(
            history,
            config.monitor,
            include_news=False,
            portfolio_holding=find_holding(portfolio_snapshot, stock.code),
            benchmark_history=benchmark_history,
            trading_habit_profile=trading_habit_profile,
            portfolio_cash_ratio=cash_ratio,
        )
        items.append(
            HistoricalAdviceItem(
                stock=stock,
                requested_at=requested_at,
                matched_at=history[-1].quote_time,
                quote=history[-1],
                result=result,
                sample_size=len(history),
            )
        )

    return items


def compare_historical_points(
    config: AppConfig,
    start_at: datetime,
    end_at: datetime,
    *,
    stocks: list[StockRef] | None = None,
) -> list[HistoricalComparisonItem]:
    if end_at <= start_at:
        raise RuntimeError("compare 的结束时间必须晚于开始时间")
    start_items = analyze_historical_point(config, start_at, stocks=stocks)
    end_items = analyze_historical_point(config, end_at, stocks=stocks)
    start_map = {item.stock.symbol: item for item in start_items}
    end_map = {item.stock.symbol: item for item in end_items}
    symbols = [stock.symbol for stock in (stocks or config.monitor.stocks)]
    comparisons: list[HistoricalComparisonItem] = []
    for symbol in symbols:
        start_item = start_map.get(symbol)
        end_item = end_map.get(symbol)
        if start_item is None or end_item is None:
            continue
        comparisons.append(HistoricalComparisonItem(stock=start_item.stock, start=start_item, end=end_item))
    return comparisons


def render_historical_advice(items: list[HistoricalAdviceItem], *, mobile: bool = False) -> str:
    if not items:
        return "未找到可分析的历史时点数据。"

    blocks: list[str] = []
    for item in items:
        note = _render_match_note(item)
        body = format_mobile_signal(item.result.title, item.result.message) if mobile else item.result.message
        blocks.append(f"{note}\n{body}".strip())
    return "\n\n".join(blocks)


def render_historical_compare(items: list[HistoricalComparisonItem], *, mobile: bool = False) -> str:
    if not items:
        return "未找到可对比的历史时点数据。"

    start_at = items[0].start.requested_at.strftime("%Y-%m-%d %H:%M:%S")
    end_at = items[0].end.requested_at.strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"【历史对比】{start_at} -> {end_at}"]
    for item in items:
        start_price = item.start.quote.current_price
        end_price = item.end.quote.current_price
        price_delta_pct = _price_delta_pct(start_price, end_price)
        start_decision = item.start.result.decision
        end_decision = item.end.result.decision
        score_delta = (end_decision.score - start_decision.score).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        lines.append(
            f"- {item.stock.code} {item.start.quote.name} | {_fmt_price(start_price)} -> {_fmt_price(end_price)} ({_signed_decimal(price_delta_pct)}%)"
        )
        lines.append(
            f"  动作 {start_decision.action} -> {end_decision.action} | 评分 {_fmt_score(start_decision.score)} -> {_fmt_score(end_decision.score)} ({_signed_decimal(score_delta)})"
        )
        lines.append(
            f"  状态 {start_decision.regime} -> {end_decision.regime} | 置信度 {start_decision.confidence} -> {end_decision.confidence}"
        )
        if not mobile:
            lines.append(f"  起点 {_render_match_note(item.start)}")
            lines.append(f"  终点 {_render_match_note(item.end)}")
        lines.append(f"  结论 {_trend_summary(item)}")
    lines.append("仅供参考，不构成投资建议")
    return "\n".join(lines)


def _load_history_for_point(
    conn,
    provider: EastmoneyMinuteHistoryProvider,
    stock: StockRef,
    requested_at: datetime,
    history_size: int,
) -> list[StockQuote]:
    cached_history = load_recent_quotes_before(conn, stock.symbol, requested_at, history_size)
    if len(cached_history) >= history_size:
        return cached_history

    fetched_quotes = provider.fetch_recent_window_exact(stock, history_size, end_time=requested_at)
    if fetched_quotes:
        cache_quotes(conn, fetched_quotes)
    history = load_recent_quotes_before(conn, stock.symbol, requested_at, history_size)
    return history


def _render_match_note(item: HistoricalAdviceItem) -> str:
    requested = item.requested_at.strftime("%Y-%m-%d %H:%M:%S")
    matched = item.matched_at.strftime("%Y-%m-%d %H:%M:%S")
    if item.exact_match:
        return f"【历史时点】请求 {requested} | 命中 {matched} | 样本 {item.sample_size}"
    return f"【历史时点】请求 {requested} | 最近可用样本 {matched} | 样本 {item.sample_size}"


def _price_delta_pct(start_price: Decimal, end_price: Decimal) -> Decimal:
    if start_price <= 0:
        return Decimal("0.00")
    return (((end_price - start_price) / start_price) * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _fmt_price(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)}"


def _fmt_score(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.00'), rounding=ROUND_HALF_UP)}"


def _signed_decimal(value: Decimal) -> str:
    scaled = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"+{scaled}" if scaled > 0 else str(scaled)


def _trend_summary(item: HistoricalComparisonItem) -> str:
    start_decision = item.start.result.decision
    end_decision = item.end.result.decision
    price_delta_pct = _price_delta_pct(item.start.quote.current_price, item.end.quote.current_price)
    action_changed = start_decision.action != end_decision.action
    score_delta = end_decision.score - start_decision.score
    if action_changed:
        return f"动作已从 {start_decision.action} 切到 {end_decision.action}，说明判断发生变化"
    if score_delta >= Decimal("6"):
        return f"评分提升 {_signed_decimal(score_delta)}，短线信号在转强"
    if score_delta <= Decimal("-6"):
        return f"评分回落 {_signed_decimal(score_delta)}，短线信号在转弱"
    if price_delta_pct > Decimal("0"):
        return "价格有抬升，但决策没有明显改善"
    if price_delta_pct < Decimal("0"):
        return "价格继续走弱，维持偏谨慎处理"
    return "两次时点变化不大，维持原有判断"


def _load_portfolio_snapshot(config: AppConfig):
    snapshot_path = config.storage.sqlite_path.resolve().parent.parent / "portfolio-snapshot.json"
    if not snapshot_path.exists():
        return None
    return load_portfolio_snapshot(snapshot_path)


def _load_benchmark_history(
    config: AppConfig,
    provider: EastmoneyMinuteHistoryProvider,
    requested_at: datetime,
) -> list[StockQuote] | None:
    benchmark = config.monitor.benchmark
    if benchmark is None:
        return None
    return provider.fetch_recent_window_exact(benchmark, config.monitor.history_size, end_time=requested_at)
