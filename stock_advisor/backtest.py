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
