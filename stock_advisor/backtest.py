from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from statistics import median

from .analysis import analyze_quotes
from .config import AppConfig, DecisionThresholds
from .models import StockQuote, StockRef
from .providers import EastmoneyMinuteHistoryProvider


DEFAULT_HORIZONS = (5, 15, 30)
OPTIMIZE_WEIGHTS = {5: 0.2, 15: 0.4, 30: 0.4}


@dataclass(slots=True)
class BacktestSample:
    symbol: str
    code: str
    signal_time: datetime
    action: str
    score: Decimal
    signal_level: str
    base_price: Decimal
    future_returns: dict[int, float]
    edge_returns: dict[int, float]


@dataclass(slots=True)
class ThresholdOptimizationCandidate:
    thresholds: DecisionThresholds
    objective: float
    stats: dict
    dominant_action_ratio: float


def run_minute_backtest(
    config: AppConfig,
    *,
    symbols: list[StockRef] | None = None,
    ndays: int = 5,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> dict:
    samples = _collect_backtest_samples(config, symbols=symbols, ndays=ndays, horizons=horizons)
    return _build_backtest_stats(
        samples,
        horizons=horizons,
        ndays=ndays,
        thresholds=config.monitor.decision_thresholds,
    )


def optimize_decision_thresholds(
    config: AppConfig,
    *,
    symbols: list[StockRef] | None = None,
    ndays: int = 5,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    top_n: int = 5,
) -> dict:
    samples = _collect_backtest_samples(config, symbols=symbols, ndays=ndays, horizons=horizons)
    baseline = _build_backtest_stats(
        samples,
        horizons=horizons,
        ndays=ndays,
        thresholds=config.monitor.decision_thresholds,
    )
    baseline_objective = round(_optimization_objective(baseline, horizons), 4)
    if not samples:
        return {
            "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "ndays": ndays,
            "sample_count": 0,
            "baseline": baseline,
            "baseline_objective": baseline_objective,
            "keep_current": True,
            "recommended": [],
        }

    candidates: list[ThresholdOptimizationCandidate] = []
    for thresholds in _candidate_thresholds(config.monitor.decision_thresholds):
        stats = _build_backtest_stats(samples, horizons=horizons, ndays=ndays, thresholds=thresholds)
        objective = _optimization_objective(stats, horizons)
        action_breakdown = stats.get("action_breakdown") or {}
        dominant_ratio = max(action_breakdown.values()) / stats["signal_count"] if action_breakdown and stats["signal_count"] else 1.0
        candidates.append(
            ThresholdOptimizationCandidate(
                thresholds=thresholds,
                objective=round(objective, 4),
                stats=stats,
                dominant_action_ratio=round(dominant_ratio, 4),
            )
        )

    ranked = sorted(
        candidates,
        key=lambda item: (
            item.objective,
            _safe_stat_value((item.stats.get("horizons") or {}).get("15", {}), "avg_edge"),
            -item.dominant_action_ratio,
        ),
        reverse=True,
    )
    recommended = []
    seen_keys: set[tuple[int, int, int]] = set()
    for item in ranked:
        thresholds_key = (
            int(item.thresholds.buy_score),
            int(item.thresholds.hold_score),
            int(item.thresholds.reduce_score),
        )
        if thresholds_key in seen_keys:
            continue
        seen_keys.add(thresholds_key)
        recommended.append(
            {
                "buy_score": int(item.thresholds.buy_score),
                "hold_score": int(item.thresholds.hold_score),
                "reduce_score": int(item.thresholds.reduce_score),
                "objective": item.objective,
                "dominant_action_ratio": item.dominant_action_ratio,
                "stats": item.stats,
            }
        )
        if len(recommended) >= top_n:
            break

    keep_current = True
    if recommended:
        best_horizons = (recommended[0]["stats"].get("horizons") or {})
        baseline_horizons = baseline.get("horizons") or {}
        best_15 = _safe_optional_value((best_horizons.get("15") or {}).get("avg_edge"))
        best_30 = _safe_optional_value((best_horizons.get("30") or {}).get("avg_edge"))
        baseline_15 = _safe_optional_value((baseline_horizons.get("15") or {}).get("avg_edge"))
        baseline_30 = _safe_optional_value((baseline_horizons.get("30") or {}).get("avg_edge"))
        keep_current = (
            recommended[0]["objective"] <= baseline_objective + 0.005
            or best_15 < baseline_15
            or best_30 < baseline_30
        )

    return {
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "ndays": ndays,
        "sample_count": len(samples),
        "baseline": baseline,
        "baseline_objective": baseline_objective,
        "keep_current": keep_current,
        "recommended": recommended,
    }


def render_optimization_report(report: dict, *, mobile: bool = False) -> str:
    lines = [f"【阈值优化】最近 {report['ndays']} 个交易日"]
    lines.append(f"样本数: {report['sample_count']}")
    baseline = report.get("baseline") or {}
    baseline_thresholds = baseline.get("decision_thresholds") or {}
    if baseline_thresholds:
        lines.append(
            "当前阈值: "
            f"buy>={baseline_thresholds.get('buy_score', '-')} | "
            f"hold>={baseline_thresholds.get('hold_score', '-')} | "
            f"reduce>={baseline_thresholds.get('reduce_score', '-')}"
        )
    baseline_15 = ((baseline.get("horizons") or {}).get("15") or {}).get("avg_edge")
    baseline_30 = ((baseline.get("horizons") or {}).get("30") or {}).get("avg_edge")
    lines.append(f"当前表现: 15分边际{_fmt_pct(baseline_15)} | 30分边际{_fmt_pct(baseline_30)}")
    lines.append(f"当前综合评分: {report.get('baseline_objective', 0.0):+.2f}")

    candidates = report.get("recommended") or []
    if not candidates:
        lines.append("没有足够样本，暂时无法给出阈值建议")
        lines.append("注：回测评分未含市场宽度/板块加权，实盘评分可能高 4-10 分")
        lines.append("仅供参考，不构成投资建议")
        return "\n".join(lines)

    lines.append("")
    if report.get("keep_current"):
        lines.append("结论: 当前阈值暂未被稳定跑赢，先保持不变")
        lines.append("候选阈值参考:")
    else:
        lines.append("建议阈值:")
    max_items = 3 if mobile else len(candidates)
    for index, item in enumerate(candidates[:max_items], start=1):
        stats = item["stats"]
        horizons = stats.get("horizons") or {}
        lines.append(
            f"{index}. buy>={item['buy_score']} hold>={item['hold_score']} reduce>={item['reduce_score']} | 综合评分{item['objective']:+.2f}"
        )
        lines.append(
            f"   15分边际{_fmt_pct((horizons.get('15') or {}).get('avg_edge'))} | 30分边际{_fmt_pct((horizons.get('30') or {}).get('avg_edge'))}"
        )
        lines.append(
            f"   动作分布 {_render_action_breakdown(stats.get('action_breakdown') or {})}"
        )

    best = candidates[0]
    if report.get("keep_current"):
        best = {
            "buy_score": baseline_thresholds.get("buy_score"),
            "hold_score": baseline_thresholds.get("hold_score"),
            "reduce_score": baseline_thresholds.get("reduce_score"),
        }
    lines.append("")
    lines.append("建议写入配置:")
    lines.append("decision_thresholds:")
    lines.append(f"  buy_score: {best['buy_score']}")
    lines.append(f"  hold_score: {best['hold_score']}")
    lines.append(f"  reduce_score: {best['reduce_score']}")
    lines.append("注：回测评分未含市场宽度/板块加权，实盘评分可能高 4-10 分")
    lines.append("注：回测评分未含市场宽度/板块加权，实盘评分可能高 4-10 分")
    lines.append("仅供参考，不构成投资建议")
    return "\n".join(lines)


def _collect_backtest_samples(
    config: AppConfig,
    *,
    symbols: list[StockRef] | None = None,
    ndays: int = 5,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> list[BacktestSample]:
    selected = symbols or config.monitor.stocks
    provider = EastmoneyMinuteHistoryProvider(config.monitor)
    benchmark = config.monitor.benchmark
    benchmark_quotes = provider.fetch_recent_days_exact(benchmark, ndays=ndays) if benchmark is not None else []
    benchmark_map = {quote.quote_time: index for index, quote in enumerate(benchmark_quotes)}

    samples: list[BacktestSample] = []

    for stock in selected:
        quotes = provider.fetch_recent_days_exact(stock, ndays=ndays)
        if len(quotes) <= config.monitor.history_size:
            continue

        for index in range(config.monitor.history_size - 1, len(quotes)):
            max_horizon = max(horizons)
            if index + max_horizon >= len(quotes):
                break
            history = quotes[index + 1 - config.monitor.history_size:index + 1]
            benchmark_history = _slice_benchmark_history(benchmark_quotes, benchmark_map, history[-1].quote_time, config.monitor.history_size)
            result = analyze_quotes(
                history,
                config.monitor,
                include_news=False,
                benchmark_history=benchmark_history,
            )
            base_price = history[-1].current_price
            future_returns: dict[int, float] = {}
            edge_returns: dict[int, float] = {}
            for horizon in horizons:
                future_price = quotes[index + horizon].current_price
                raw_return = _pct_return(base_price, future_price)
                future_returns[horizon] = raw_return
                edge_returns[horizon] = _strategy_edge(result.decision.action, raw_return)

            samples.append(
                BacktestSample(
                    symbol=stock.symbol,
                    code=stock.code,
                    signal_time=history[-1].quote_time,
                    action=result.decision.action,
                    score=result.decision.score,
                    signal_level=result.signal_level,
                    base_price=base_price,
                    future_returns=future_returns,
                    edge_returns=edge_returns,
                )
            )
    return samples


def _build_backtest_stats(
    samples: list[BacktestSample],
    *,
    horizons: tuple[int, ...],
    ndays: int,
    thresholds: DecisionThresholds,
) -> dict:
    action_breakdown: dict[str, int] = {}
    score_sum = Decimal("0")
    grouped: dict[str, list[BacktestSample]] = {}
    remapped_samples: list[tuple[BacktestSample, str]] = []

    for sample in samples:
        action = _decision_action_for_score(sample.score, thresholds)
        remapped_samples.append((sample, action))
        action_breakdown[action] = action_breakdown.get(action, 0) + 1
        grouped.setdefault(action, []).append(sample)
        score_sum += sample.score

    summaries = {str(h): _summarize_horizon(remapped_samples, h) for h in horizons}
    by_action: dict[str, dict[str, dict]] = {}
    for action in ("buy", "hold", "reduce", "avoid"):
        group = grouped.get(action)
        if not group:
            continue
        by_action[action] = {str(h): _summarize_horizon([(sample, action) for sample in group], h) for h in horizons}
    return {
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "ndays": ndays,
        "signal_count": len(samples),
        "avg_score": float((score_sum / Decimal(len(samples))).quantize(Decimal("0.01"))) if samples else None,
        "action_breakdown": action_breakdown,
        "decision_thresholds": {
            "buy_score": int(thresholds.buy_score),
            "hold_score": int(thresholds.hold_score),
            "reduce_score": int(thresholds.reduce_score),
        },
        "horizons": summaries,
        "by_action": by_action,
    }


def render_minute_backtest(stats: dict, *, mobile: bool = False) -> str:
    lines = [f"【分钟级回测】最近 {stats['ndays']} 个交易日"]
    lines.append(f"样本数: {stats['signal_count']}")
    if stats.get("avg_score") is not None:
        lines.append(f"平均分: {stats['avg_score']:.2f}")
    decision_thresholds = stats.get("decision_thresholds") or {}
    if decision_thresholds:
        lines.append(
            "动作阈值: "
            f"buy>={decision_thresholds.get('buy_score', '-')} | "
            f"hold>={decision_thresholds.get('hold_score', '-')} | "
            f"reduce>={decision_thresholds.get('reduce_score', '-')}"
        )
    breakdown = stats.get("action_breakdown") or {}
    if breakdown:
        lines.append("动作分布: " + _render_action_breakdown(breakdown))

    for horizon, summary in (stats.get("horizons") or {}).items():
        lines.append(
            f"{horizon}分后 -> 原始收益均值{_fmt_pct(summary['avg_raw'])} 胜率{_fmt_pct(summary['win_rate_raw'])} | 策略边际均值{_fmt_pct(summary['avg_edge'])} 正确率{_fmt_pct(summary['win_rate_edge'])}"
        )

    by_action = stats.get("by_action") or {}
    if mobile and by_action:
        first_action_stats = next(iter(by_action.values()), {})
        focus = "15" if "15" in first_action_stats else next(iter(first_action_stats.keys()), None)
        if focus is not None:
            lines.append(f"{focus}分动作拆解:")
            for action, action_stats in by_action.items():
                summary = action_stats.get(focus) or {}
                lines.append(
                    f"{action}: {summary.get('samples', 0)}笔 | 边际{_fmt_pct(summary.get('avg_edge'))} | 正确率{_fmt_pct(summary.get('win_rate_edge'))}"
                )
    else:
        for action, action_stats in (stats.get("by_action") or {}).items():
            lines.append(f"[{action}]")
            for horizon, summary in action_stats.items():
                lines.append(
                    f"  {horizon}分后: 样本{summary['samples']} 原始均值{_fmt_pct(summary['avg_raw'])} 中位{_fmt_pct(summary['median_raw'])} | 边际均值{_fmt_pct(summary['avg_edge'])}"
                )
    lines.append("仅供参考，不构成投资建议")
    return "\n".join(lines)


def _slice_benchmark_history(
    benchmark_quotes: list[StockQuote],
    benchmark_map: dict[datetime, int],
    quote_time: datetime,
    history_size: int,
) -> list[StockQuote] | None:
    if not benchmark_quotes:
        return None
    index = benchmark_map.get(quote_time)
    if index is None:
        return None
    start = max(0, index + 1 - history_size)
    return benchmark_quotes[start:index + 1]


def _pct_return(base_price: Decimal, future_price: Decimal) -> float:
    if base_price <= 0:
        return 0.0
    return float(((future_price - base_price) / base_price * Decimal("100")).quantize(Decimal("0.0001")))


ROUND_TRIP_COST = 0.0010  # 0.10% round-trip (0.05% each side, typical A-share)


def _strategy_edge(action: str, raw_return: float) -> float:
    directional = raw_return if action in {"buy", "hold"} else -raw_return
    return directional - ROUND_TRIP_COST


def _summarize_horizon(samples: list[tuple[BacktestSample, str]], horizon: int) -> dict:
    raw_values = [sample.future_returns[horizon] for sample, _action in samples if horizon in sample.future_returns]
    edge_values = [
        _strategy_edge(action, sample.future_returns[horizon])
        for sample, action in samples
        if horizon in sample.future_returns
    ]
    if not raw_values or not edge_values:
        return {
            "samples": 0,
            "avg_raw": None,
            "median_raw": None,
            "win_rate_raw": None,
            "avg_edge": None,
            "median_edge": None,
            "win_rate_edge": None,
        }
    return {
        "samples": len(raw_values),
        "avg_raw": round(sum(raw_values) / len(raw_values), 4),
        "median_raw": round(median(raw_values), 4),
        "win_rate_raw": round(sum(1 for value in raw_values if value > 0) / len(raw_values) * 100, 2),
        "avg_edge": round(sum(edge_values) / len(edge_values), 4),
        "median_edge": round(median(edge_values), 4),
        "win_rate_edge": round(sum(1 for value in edge_values if value > 0) / len(edge_values) * 100, 2),
    }


def _decision_action_for_score(score: Decimal, thresholds: DecisionThresholds) -> str:
    if score >= Decimal(str(thresholds.buy_score)):
        return "buy"
    if score >= Decimal(str(thresholds.hold_score)):
        return "hold"
    if score >= Decimal(str(thresholds.reduce_score)):
        return "reduce"
    return "avoid"


def _candidate_thresholds(current: DecisionThresholds) -> list[DecisionThresholds]:
    candidates: list[DecisionThresholds] = []
    for buy_score in range(70, 91, 2):
        for hold_score in range(50, 75, 2):
            if hold_score >= buy_score:
                continue
            for reduce_score in range(28, 53, 2):
                if reduce_score >= hold_score:
                    continue
                if buy_score - hold_score < 8 or hold_score - reduce_score < 8:
                    continue
                candidates.append(
                    DecisionThresholds(
                        buy_score=float(buy_score),
                        hold_score=float(hold_score),
                        reduce_score=float(reduce_score),
                    )
                )
    candidates.append(current)
    return candidates


def _optimization_objective(stats: dict, horizons: tuple[int, ...]) -> float:
    summaries = stats.get("horizons") or {}
    total_weight = 0.0
    weighted_edge = 0.0
    for horizon in horizons:
        summary = summaries.get(str(horizon)) or {}
        avg_edge = summary.get("avg_edge")
        if avg_edge is None:
            continue
        weight = OPTIMIZE_WEIGHTS.get(horizon, 0.0)
        total_weight += weight
        weighted_edge += avg_edge * weight
    if total_weight == 0:
        return float("-inf")

    action_breakdown = stats.get("action_breakdown") or {}
    total = max(stats.get("signal_count") or 0, 1)
    dominant_ratio = max(action_breakdown.values()) / total if action_breakdown else 1.0
    buy_count = action_breakdown.get("buy", 0)
    hold_count = action_breakdown.get("hold", 0)
    reduce_count = action_breakdown.get("reduce", 0)
    avoid_count = action_breakdown.get("avoid", 0)

    objective = weighted_edge / total_weight
    if dominant_ratio > 0.78:
        objective -= (dominant_ratio - 0.78) * 0.08
    if hold_count == 0:
        objective -= 0.01
    if reduce_count == 0:
        objective -= 0.02
    if (buy_count + hold_count) / total < 0.03:
        objective -= 0.02
    if avoid_count / total > 0.60:
        objective -= 0.02
    return objective


def _render_action_breakdown(breakdown: dict[str, int]) -> str:
    ordered = ("buy", "hold", "reduce", "avoid")
    parts = [f"{action}:{breakdown[action]}" for action in ordered if action in breakdown]
    parts.extend(f"{action}:{count}" for action, count in breakdown.items() if action not in ordered)
    return " | ".join(parts)


def _safe_stat_value(summary: dict, key: str) -> float:
    value = summary.get(key)
    return float("-inf") if value is None else float(value)


def _safe_optional_value(value: float | None) -> float:
    return float("-inf") if value is None else float(value)


def _fmt_pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}%"


# ── Daily-level backtest (item #5: 回测框架) ──

@dataclass(slots=True)
class DailyBacktestResult:
    """日线级别回测结果 — 验证每日评分策略"""
    symbol: str
    start_date: str
    end_date: str
    total_signals: int
    buy_signals: int
    hold_signals: int
    reduce_signals: int
    avoid_signals: int
    avg_score: Decimal
    daily_returns: list[dict]
    summary: str


def run_daily_backtest(
    config: AppConfig,
    stock: StockRef,
    days: int = 60,
    *,
    initial_cost: Decimal | None = None,
    initial_quantity: int = 0,
) -> DailyBacktestResult:
    """Run backtest on a single stock using daily scoring data."""
    from collections import defaultdict
    from datetime import date, timedelta

    from .models import PortfolioHolding

    provider = EastmoneyMinuteHistoryProvider(config.monitor)
    end_date = date.today()
    start_date = end_date - timedelta(days=days * 2)

    # Fetch daily closes/volumes for daily scoring
    daily_closes: list[Decimal] | None = None
    daily_volumes: list[Decimal] | None = None
    try:
        daily_closes, daily_volumes = provider.fetch_daily_klines(stock, ndays=max(days, 60))
    except Exception:
        pass

    # Fetch minute history per day
    all_quotes = provider.fetch_quotes(stock, start_date, end_date)
    if not all_quotes:
        raise RuntimeError(f"No historical data for {stock.symbol}")

    day_groups: dict[date, list[StockQuote]] = defaultdict(list)
    for q in all_quotes:
        day_groups[q.quote_time.date()].append(q)

    trading_dates = sorted(day_groups.keys())[-days:]

    signals: list[dict] = []
    total_score = Decimal("0")
    buy_count = hold_count = reduce_count = avoid_count = 0

    holding = PortfolioHolding(
        name=stock.code,
        code=stock.code,
        quantity=initial_quantity,
        cost_price=initial_cost or Decimal("0"),
        current_price=Decimal("0"),
    ) if initial_quantity > 0 else None

    for i, td in enumerate(trading_dates):
        quotes = day_groups[td]
        if len(quotes) < 5:
            continue

        try:
            result = analyze_quotes(
                quotes,
                config.monitor,
                portfolio_holding=holding,
                include_news=False,
                daily_closes=daily_closes[:i+1] if daily_closes else None,
                daily_volumes=daily_volumes[:i+1] if daily_volumes else None,
            )
        except Exception:
            continue

        action = result.decision.action
        score = result.decision.score
        total_score += score

        if action == "buy":
            buy_count += 1
        elif action == "hold":
            hold_count += 1
        elif action == "reduce":
            reduce_count += 1
        else:
            avoid_count += 1

        next_day_return = Decimal("0")
        if i + 1 < len(trading_dates):
            next_quotes = day_groups[trading_dates[i + 1]]
            if next_quotes:
                next_close = next_quotes[-1].current_price
                today_close = quotes[-1].current_price
                if today_close > 0:
                    next_day_return = ((next_close - today_close) / today_close * 100).quantize(Decimal("0.01"))

        signals.append({
            "date": td.isoformat(),
            "close": float(quotes[-1].current_price),
            "action": action,
            "score": float(score),
            "confidence": result.decision.confidence,
            "next_day_return": float(next_day_return),
            "rationale": result.decision.rationale[:2],
        })

    total = buy_count + hold_count + reduce_count + avoid_count
    avg_score = (total_score / Decimal(str(total))).quantize(Decimal("0.01")) if total > 0 else Decimal("50")

    buy_signals_list = [s for s in signals if s["action"] == "buy"]
    winning_buys = sum(1 for s in buy_signals_list if s["next_day_return"] > 0)
    win_rate = (winning_buys / len(buy_signals_list) * 100) if buy_signals_list else 0

    summary_lines = [
        f"日线回测 {stock.symbol} | {trading_dates[0]} → {trading_dates[-1]}",
        f"交易日: {len(trading_dates)} | 有效信号: {total}",
        f"买入: {buy_count} | 持有: {hold_count} | 减仓: {reduce_count} | 观望: {avoid_count}",
        f"平均评分: {avg_score}",
    ]
    if buy_signals_list:
        summary_lines.append(f"买入次日胜率: {win_rate:.1f}% ({winning_buys}/{len(buy_signals_list)})")

    return DailyBacktestResult(
        symbol=stock.symbol,
        start_date=trading_dates[0].isoformat(),
        end_date=trading_dates[-1].isoformat(),
        total_signals=total,
        buy_signals=buy_count,
        hold_signals=hold_count,
        reduce_signals=reduce_count,
        avoid_signals=avoid_count,
        avg_score=avg_score,
        daily_returns=signals,
        summary="\\n".join(summary_lines),
    )


# ═══════════════════════════════════════════════════════════════════
# Enhanced backtest (borrowed from daily_stock_analysis)
# Adds: multi-horizon returns, direction accuracy, all-signal eval
# ═══════════════════════════════════════════════════════════════════

HORIZONS = (1, 3, 5)  # trading days


def _multi_horizon_return(
    trading_dates: list,
    day_groups: dict,
    today_idx: int,
    horizon: int,
) -> float | None:
    """Return % price change from today's close to horizon days later."""
    future_idx = today_idx + horizon
    if future_idx >= len(trading_dates):
        return None
    today_close = float(day_groups[trading_dates[today_idx]][-1].current_price)
    future_close = float(day_groups[trading_dates[future_idx]][-1].current_price)
    if today_close <= 0:
        return None
    return (future_close - today_close) / today_close * 100


def direction_accuracy(
    action: str,
    returns: dict[int, float | None],
    neutral_band: float = 1.0,
) -> dict:
    """Check if price moved in the direction predicted by the action.

    action → expected direction:
      buy    → up (> +neutral_band%)
      hold   → not_down (> -neutral_band%)
      reduce → down (< -neutral_band%)
      avoid  → not_up (< +neutral_band%)

    Returns {horizon: 'win'|'loss'|'neutral'|None}
    """
    expected = {
        "buy": "up",
        "hold": "not_down",
        "reduce": "down",
        "avoid": "not_up",
    }.get(action, "flat")

    result = {}
    for h in HORIZONS:
        r = returns.get(h)
        if r is None:
            result[h] = None
            continue

        if expected == "up":
            result[h] = "win" if r >= neutral_band else ("loss" if r <= -neutral_band else "neutral")
        elif expected == "down":
            result[h] = "win" if r <= -neutral_band else ("loss" if r >= neutral_band else "neutral")
        elif expected == "not_down":
            result[h] = "win" if r > -neutral_band else "loss"
        elif expected == "not_up":
            result[h] = "win" if r < neutral_band else "loss"
        else:
            result[h] = "win" if abs(r) <= neutral_band else "loss"
    return result


def _build_enhanced_summary(
    signals: list[dict],
    action_breakdown: dict[str, dict],
) -> list[str]:
    """Build enhanced summary with multi-horizon win rates and direction metrics."""
    total = len(signals)
    if total == 0:
        return ["无有效信号"]

    lines = ["── 增强回测报告 ──", f"总信号: {total}"]

    # Per-action breakdown
    for action in ["buy", "hold", "reduce", "avoid"]:
        bd = action_breakdown.get(action, {})
        cnt = bd.get("count", 0)
        if cnt == 0:
            continue
        lines.append(f"\n【{action}】共 {cnt} 次")
        for h in HORIZONS:
            wins = bd.get(f"wins_{h}d", 0)
            lines.append(f"  {h}日方向正确: {wins}/{cnt} ({wins/cnt*100:.0f}%)")
        # Average return
        for h in HORIZONS:
            returns = bd.get(f"returns_{h}d", [])
            if returns:
                avg = sum(returns) / len(returns)
                lines.append(f"  {h}日平均收益: {avg:+.2f}%")

    # Overall
    lines.append("\n── 综合 ──")
    for h in HORIZONS:
        total_wins = sum(
            bd.get(f"wins_{h}d", 0) for bd in action_breakdown.values()
        )
        lines.append(f"{h}日总体方向正确率: {total_wins}/{total} ({total_wins/total*100:.0f}%)")

    return lines


def run_enhanced_daily_backtest(
    config,
    stock,
    days: int = 60,
    *,
    initial_cost: Decimal | None = None,
    initial_quantity: int = 0,
) -> dict:
    """Enhanced backtest: multi-horizon + direction accuracy + all-signal eval.

    Unlike the original run_daily_backtest (which only checks buy next-day win rate),
    this evaluates every signal across 1d/3d/5d horizons and checks if the price
    moved in the direction predicted by the action.
    """
    from collections import defaultdict
    from datetime import date, timedelta

    from .models import PortfolioHolding

    provider = EastmoneyMinuteHistoryProvider(config.monitor)
    end_date = date.today()
    start_date = end_date - timedelta(days=days * 2)

    daily_closes: list[Decimal] | None = None
    daily_volumes: list[Decimal] | None = None
    try:
        daily_closes, daily_volumes = provider.fetch_daily_klines(stock, ndays=max(days, 60))
    except Exception:
        pass

    all_quotes = provider.fetch_quotes(stock, start_date, end_date)
    if not all_quotes:
        raise RuntimeError(f"No historical data for {stock.symbol}")

    day_groups: dict[date, list] = defaultdict(list)
    for q in all_quotes:
        day_groups[q.quote_time.date()].append(q)

    trading_dates = sorted(day_groups.keys())[-days:]

    holding = PortfolioHolding(
        name=stock.code, code=stock.code,
        quantity=initial_quantity,
        cost_price=initial_cost or Decimal("0"),
        current_price=Decimal("0"),
    ) if initial_quantity > 0 else None

    signals = []
    # Accumulators for per-action breakdown
    action_breakdown: dict[str, dict] = defaultdict(lambda: {
        "count": 0,
        "returns_1d": [], "returns_3d": [], "returns_5d": [],
        "wins_1d": 0, "wins_3d": 0, "wins_5d": 0,
    })

    for i, td in enumerate(trading_dates):
        quotes = day_groups[td]
        if len(quotes) < 5:
            continue

        try:
            result = analyze_quotes(
                quotes, config.monitor,
                portfolio_holding=holding,
                include_news=False,
                daily_closes=daily_closes[:i+1] if daily_closes else None,
                daily_volumes=daily_volumes[:i+1] if daily_volumes else None,
            )
        except Exception:
            continue

        action = result.decision.action
        today_close = float(quotes[-1].current_price)

        # Multi-horizon returns
        returns = {}
        for h in HORIZONS:
            returns[h] = _multi_horizon_return(trading_dates, day_groups, i, h)

        # Direction accuracy
        acc = direction_accuracy(action, returns)

        signal = {
            "date": td.isoformat(),
            "close": today_close,
            "action": action,
            "score": float(result.decision.score),
            "confidence": result.decision.confidence,
        }
        for h in HORIZONS:
            r = returns.get(h)
            signal[f"return_{h}d"] = round(r, 2) if r is not None else None
            signal[f"direction_{h}d"] = acc.get(h)

        signals.append(signal)

        # Update breakdown
        bd = action_breakdown[action]
        bd["count"] += 1
        for h in HORIZONS:
            r = returns.get(h)
            if r is not None:
                bd[f"returns_{h}d"].append(r)
            if acc.get(h) == "win":
                bd[f"wins_{h}d"] += 1

    summary_lines = _build_enhanced_summary(signals, dict(action_breakdown))

    return {
        "symbol": stock.symbol,
        "start": trading_dates[0].isoformat(),
        "end": trading_dates[-1].isoformat(),
        "trading_days": len(trading_dates),
        "signals": signals,
        "breakdown": {k: {
            "count": v["count"],
            "wins_1d": v["wins_1d"],
            "wins_3d": v["wins_3d"],
            "wins_5d": v["wins_5d"],
            "avg_return_1d": round(sum(v["returns_1d"])/len(v["returns_1d"]), 2) if v["returns_1d"] else None,
            "avg_return_3d": round(sum(v["returns_3d"])/len(v["returns_3d"]), 2) if v["returns_3d"] else None,
            "avg_return_5d": round(sum(v["returns_5d"])/len(v["returns_5d"]), 2) if v["returns_5d"] else None,
        } for k, v in action_breakdown.items()},
        "summary": "\n".join(summary_lines),
    }
