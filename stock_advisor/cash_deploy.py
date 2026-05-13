"""
现金部署信号 — 基于 cash_deploy.yaml 策略的自动化买入条件检查
适用场景：现金占比 > 60%，寻找安全入场时机

用法：
  python3 -m stock_advisor.cli cash-deploy --config config.yaml
  python3 -m stock_advisor.cli cash-deploy --config config.yaml --mobile
  python3 -m stock_advisor.cli cash-deploy --config config.yaml --notify
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from statistics import mean

from .config import AppConfig
from .market_hours import MARKET_TZ
from .models import StockRef
from .portfolio import compute_cash_ratio, load_snapshot
from .providers import EastmoneyMinuteHistoryProvider, TencentQuoteProvider


@dataclass(slots=True)
class DeployCondition:
    """单只股票的部署条件检查结果"""

    symbol: str
    name: str
    current_price: Decimal
    change_pct: float
    conditions: list[tuple[str, bool, str]]
    # condition: (名称, 是否满足, 详情)
    conditions_met: int
    conditions_total: int
    deployable: bool
    score: int  # 0-10，越高越适合部署
    verdict: str  # 综合判断
    suggested_lot: int = 0  # 建议买入股数
    suggested_price: Decimal | None = None
    suggested_amount: Decimal | None = None  # 建议买入金额
    warnings: list[str] | None = None


@dataclass(slots=True)
class CashDeploySignal:
    """全市场现金部署信号"""

    generated_at: str
    cash_pct: float
    cash_amount: Decimal
    total_assets: Decimal
    benchmark_price: Decimal
    benchmark_change_pct: float
    advance_ratio: float | None  # 上涨家数占比
    market_verdict: str  # "安全" / "谨慎" / "危险"
    deployable_targets: list[DeployCondition]
    summary: str


def generate_deploy_signal(
    config: AppConfig,
    *,
    mobile: bool = False,
) -> CashDeploySignal:
    """生成现金部署信号。

    检查市场环境和各标的的部署条件，输出优先级排序的部署建议。
    """
    snapshot = load_snapshot(config.snapshot_path)
    cash_ratio = compute_cash_ratio(snapshot)
    cash_pct = float(cash_ratio * 100) if cash_ratio is not None else 0.0  # 百分比

    tencent = TencentQuoteProvider(config.monitor)

    # 1. 市场环境检查
    benchmark_ref = config.monitor.benchmark
    if benchmark_ref is None:
        benchmark_ref = StockRef("sh", "000001")
    benchmark_quote = tencent.fetch_quote(benchmark_ref)
    benchmark_change = float(benchmark_quote.change_percent)

    # 上涨家数占比（简化版，如果API可用）
    advance_ratio = _fetch_advance_ratio(config)

    market_verdict, market_ok = _assess_market(
        benchmark_change, advance_ratio
    )

    # 2. 逐标的检查
    provider = EastmoneyMinuteHistoryProvider(config.monitor)
    deployable_targets: list[DeployCondition] = []

    for stock in config.monitor.stocks:
        holding = next(
            (h for h in snapshot.holdings if h.code == stock.code),
            None,
        )
        # 跳过已清仓且不在持仓中的标的
        if holding is None:
            continue  # 不在 snapshot 中，可能是新加的监控标的，也跳过
        if holding.quantity <= 0:
            continue  # 已清仓
        result = _check_stock_deploy(
            stock=stock,
            provider=provider,
            config=config,
            holding=holding,
            cash_pct=cash_pct,
            total_assets=snapshot.total_assets,
            benchmark_change=benchmark_change,
            advance_ratio=advance_ratio,
            market_ok=market_ok,
        )
        if result is not None:
            deployable_targets.append(result)

    # 3. 按评分排序
    deployable_targets.sort(key=lambda t: t.score, reverse=True)

    # 4. 生成摘要
    deployable = [t for t in deployable_targets if t.deployable]
    if not deployable:
        reasons = []
        if not market_ok:
            reasons.append(f"市场「{market_verdict}」")
        if cash_pct <= 60:
            reasons.append(f"现金占比 {cash_pct:.0f}% 不足 60%，不触发部署信号")
        if deployable_targets:
            no_deploy = [t.name for t in deployable_targets if not t.deployable]
            if no_deploy:
                reasons.append("、".join(no_deploy) + " 不满足条件")
        summary = f"现金部署信号：不部署\n原因：{'；'.join(reasons) if reasons else '无满足条件的标的'}"
    else:
        top = deployable[0]
        suggested_amount = _suggested_amount(snapshot.total_assets, cash_pct)
        selected = deployable[: min(2, len(deployable))]
        target_desc = "、".join(
            f"{t.name}({t.verdict[:min(8, len(t.verdict))]}…)" for t in selected
        )
        summary = (
            f"现金部署信号：可部署\n"
            f"市场「{market_verdict}」| 现金 {cash_pct:.0f}%（{snapshot.cash}元）\n"
            f"优先标的：{target_desc}\n"
            f"建议单次入场 {suggested_amount} 元"
        )

    return CashDeploySignal(
        generated_at=datetime.now(MARKET_TZ).isoformat(sep=" ", timespec="seconds"),
        cash_pct=round(cash_pct, 1),
        cash_amount=snapshot.cash,
        total_assets=snapshot.total_assets,
        benchmark_price=benchmark_quote.current_price,
        benchmark_change_pct=benchmark_change,
        advance_ratio=advance_ratio,
        market_verdict=market_verdict,
        deployable_targets=deployable_targets,
        summary=summary,
    )


def _assess_market(
    benchmark_change: float,
    advance_ratio: float | None,
) -> tuple[str, bool]:
    """评估市场环境是否适合部署现金。

    Returns:
        (verdict, is_safe) — verdict 是人类可读标签，is_safe 表示可以继续检查个股。
    """
    if benchmark_change <= -3.5:
        return "暴跌⚠️", False
    if benchmark_change <= -1.5:
        if advance_ratio is not None and advance_ratio < 0.35:
            return "下跌+普跌⚠️", False
        return "偏弱⚠️", False
    if benchmark_change <= -0.5:
        return "偏弱", True
    if benchmark_change <= 0.5:
        return "震荡", True
    if benchmark_change <= 1.5:
        return "偏强", True
    return "强势", True


def _check_stock_deploy(
    stock: StockRef,
    provider: EastmoneyMinuteHistoryProvider,
    config: AppConfig,
    holding,  # PortfolioHolding | None
    cash_pct: float,
    total_assets: Decimal,
    benchmark_change: float,
    advance_ratio: float | None,
    market_ok: bool,
) -> DeployCondition | None:
    """检查单只股票是否满足现金部署条件。"""
    try:
        daily_closes, daily_volumes = provider.fetch_daily_klines(stock, ndays=60)
    except Exception:
        return None

    if not daily_closes or len(daily_closes) < 20:
        return None

    tencent = TencentQuoteProvider(config.monitor)
    try:
        quote = tencent.fetch_quote(stock)
    except Exception:
        return None

    current_price = quote.current_price
    change_pct = float(quote.change_percent)
    name = quote.name or stock.code
    conditions: list[tuple[str, bool, str]] = []

    # 检查持仓状态
    pnl_pct: float | None = None
    if holding and holding.quantity > 0 and holding.cost_price > 0:
        pnl_pct = float(
            ((current_price - holding.cost_price) / holding.cost_price * 100)
        )

    # === 条件检查（对应 cash_deploy.yaml 8 条核心规则）===

    # 条件1: 大盘环境安全
    if market_ok:
        conditions.append(("大盘安全", True, f"基准 {benchmark_change:+.2f}%"))
    else:
        conditions.append(("大盘安全", False, f"基准 {benchmark_change:+.2f}%，不满足"))

    # 条件2: 趋势确认 — MA5 > MA10 > MA20
    ma5 = _sma(daily_closes, 5)
    ma10 = _sma(daily_closes, 10)
    ma20 = _sma(daily_closes, 20)
    ma_bullish = (ma5 > ma10 > ma20) and current_price > ma20
    if ma_bullish:
        conditions.append(("多头排列", True, f"MA5>{_fmt_ma(ma5)} MA10>{_fmt_ma(ma10)} MA20>{_fmt_ma(ma20)}"))
    else:
        conditions.append(("多头排列", False, "MA排列不满足 MA5>MA10>MA20"))

    # 条件3: RSI 30-70
    rsi14 = _rsi(daily_closes, 14)
    if 30 <= rsi14 <= 70:
        conditions.append(("RSI适中", True, f"RSI14={rsi14:.0f}"))
    else:
        conditions.append(("RSI适中", False, f"RSI14={rsi14:.0f}（{'过热' if rsi14 > 70 else '过冷'}）"))

    # 条件4: 不追高 — 日涨幅 ≤3%
    if abs(change_pct) <= 3:
        conditions.append(("不追高", True, f"日涨幅 {change_pct:+.2f}%"))
    else:
        conditions.append(("不追高", False, f"日涨幅 {change_pct:+.2f}%，≥3%不追"))

    # 条件5: 近5日涨幅 ≤15%
    chg_5d = _n_day_return(daily_closes, 5)
    if chg_5d <= 15:
        conditions.append(("5日涨幅可控", True, f"5日涨幅 {chg_5d:+.1f}%"))
    else:
        conditions.append(
            ("5日涨幅可控", False, f"5日涨幅 {chg_5d:+.1f}%，≥15% 等回踩")
        )

    # 条件6: 价格在 MA20 之上
    price_above_ma20 = current_price > ma20
    conditions.append(
        ("价格>MA20", price_above_ma20, f"现价{current_price} vs MA20={_fmt_ma(ma20)}")
    )

    # 条件7: 不是深套股
    if pnl_pct is None or pnl_pct > -20:
        conditions.append(("非深套", True, f"盈亏 {pnl_pct:+.1f}%" if pnl_pct else "无持仓"))
    else:
        conditions.append(("非深套", False, f"盈亏 {pnl_pct:+.1f}%，深套不补"))

    # 条件8: 仓位控制 — 单票不超过35%
    if holding and holding.quantity > 0:
        pos_value = current_price * holding.quantity
        pos_pct = float(pos_value / total_assets * 100)
        if pos_pct <= 35:
            conditions.append(("仓位可控", True, f"单票 {pos_pct:.0f}%"))
        else:
            conditions.append(("仓位可控", False, f"单票 {pos_pct:.0f}%，>35%不补"))
    else:
        conditions.append(("仓位可控", True, "无持仓"))

    conditions_met = sum(1 for _, ok, _ in conditions if ok)
    conditions_total = len(conditions)

    # 计算评分 (0-10)
    score = conditions_met
    # 加分项
    if ma_bullish:
        score += 1
    if pnl_pct is not None and pnl_pct > 0:
        score += 0.5  # 浮盈加分
    # 扣分项
    if not market_ok:
        score -= 2

    deployable = conditions_met >= 5 and market_ok

    # 硬拦截：深套股永不补仓
    if pnl_pct is not None and pnl_pct < -20:
        deployable = False

    # 硬拦截：涨幅≥3%不追
    if abs(change_pct) >= 3:
        deployable = False

    # 生成 verdict
    if deployable and score >= 6:
        verdict = f"✅ 优先部署（{conditions_met}/{conditions_total}条件）"
        suggested_lot = _calc_lot(current_price, total_assets, cash_pct)
        suggested_amount = current_price * suggested_lot
    elif deployable:
        verdict = f"🟡 可考虑（{conditions_met}/{conditions_total}条件）"
        suggested_lot = _calc_lot(current_price, total_assets, cash_pct) // 2
        suggested_amount = current_price * suggested_lot
    elif conditions_met >= 4:
        verdict = f"⏸️ 待观察（{conditions_met}/{conditions_total}条件）"
        suggested_lot = 0
        suggested_amount = Decimal("0")
    else:
        verdict = f"❌ 暂不部署（{conditions_met}/{conditions_total}条件）"
        suggested_lot = 0
        suggested_amount = Decimal("0")

    warnings = None
    if not deployable and conditions_met >= 4:
        failed = [name for name, ok, _ in conditions if not ok]
        warnings = [f"不满足: {', '.join(failed)}"]

    return DeployCondition(
        symbol=stock.symbol,
        name=name,
        current_price=current_price,
        change_pct=round(change_pct, 2),
        conditions=conditions,
        conditions_met=conditions_met,
        conditions_total=conditions_total,
        deployable=deployable,
        score=min(int(score), 10),
        verdict=verdict,
        suggested_lot=suggested_lot,
        suggested_price=current_price,
        suggested_amount=suggested_amount,
        warnings=warnings,
    )


def _fetch_advance_ratio(config: AppConfig) -> float | None:
    """获取全市场上涨家数占比（简化版）。"""
    try:
        from .providers import EastmoneyMarketSnapshotProvider

        provider = EastmoneyMarketSnapshotProvider(config.monitor)
        boards = provider.fetch_all_boards()
        if boards and "a_stock" in boards:
            a_data = boards["a_stock"]
            up = a_data.get("up_count", 0)
            dn = a_data.get("down_count", 1)
            total = up + dn
            if total > 0:
                return round(up / total * 100, 1)
    except Exception:
        pass
    return None


def _sma(closes: list[Decimal], period: int) -> Decimal:
    """简单移动平均"""
    if len(closes) < period:
        return Decimal("0")
    return sum(closes[-period:]) / period


def _rsi(closes: list[Decimal], period: int = 14) -> float:
    """RSI 计算"""
    if len(closes) < period + 1:
        return 50.0
    gains = Decimal("0")
    losses = Decimal("0")
    for i in range(-period, 0):
        change = closes[i] - closes[i - 1]
        if change > 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = float(avg_gain / avg_loss)
    return 100 - (100 / (1 + rs))


def _n_day_return(closes: list[Decimal], ndays: int) -> float:
    """N日涨跌幅 %"""
    if len(closes) < ndays + 1:
        return 0.0
    return float(
        ((closes[-1] - closes[-ndays - 1]) / closes[-ndays - 1] * 100)
    )


def _suggested_amount(total_assets: Decimal, cash_pct: float) -> int:
    """建议单次入场金额"""
    if total_assets < 50000:
        return 3000
    elif total_assets < 100000:
        return 5000
    else:
        return min(int(total_assets * Decimal("0.08")), 8000)


def _calc_lot(
    price: Decimal, total_assets: Decimal, cash_pct: float
) -> int:
    """计算建议买入股数（按100股整数倍）"""
    amount = _suggested_amount(total_assets, cash_pct)
    if price <= 0:
        return 0
    shares = int(amount / float(price))
    # 取整到100股
    shares = (shares // 100) * 100
    return max(100, shares)


def _fmt_ma(ma: Decimal) -> str:
    return f"{float(ma):.2f}"


def render_deploy_signal(signal: CashDeploySignal, *, mobile: bool = False) -> str:
    """渲染现金部署信号为人类可读文本。"""
    lines = [
        f"【现金部署信号】",
        f"生成时间：{signal.generated_at}",
        "",
        f"💰 账户：总资产 {signal.total_assets} 元 | 现金 {signal.cash_amount} 元（{signal.cash_pct:.0f}%）",
        f"📊 上证：{signal.benchmark_price}（{signal.benchmark_change_pct:+.2f}%）| 市场「{signal.market_verdict}」",
    ]
    if signal.advance_ratio is not None:
        lines.append(f"   上涨家数占比：{signal.advance_ratio:.0f}%")

    lines.append("")
    lines.append("─" * 30)

    if not signal.deployable_targets:
        lines.append("无标的数据")
    else:
        for target in signal.deployable_targets:
            pnl_info = ""
            if target.conditions:
                # 从 conditions 找持仓盈亏
                for c in target.conditions:
                    if c[0] == "非深套" and "盈亏" in c[2]:
                        pnl_info = f" | {c[2]}"
                        break

            lines.append(
                f"{target.name}({target.symbol}) "
                f"现价 {target.current_price}（{target.change_pct:+.2f}%）{pnl_info}"
            )
            lines.append(f"  部署评分：{target.score}/10 | {target.verdict}")

            if target.deployable and target.suggested_lot > 0:
                lines.append(
                    f"  建议买入：{target.suggested_lot}股 @ {target.suggested_price} "
                    f"≈ {target.suggested_amount}元"
                )

            if not mobile:
                lines.append("  条件检查：")
                for cond_name, ok, detail in target.conditions:
                    mark = "✓" if ok else "✗"
                    lines.append(f"    {mark} {cond_name}：{detail}")

            if target.warnings:
                for w in target.warnings:
                    lines.append(f"  ⚠️ {w}")

            if mobile:
                lines.append("")

    lines.append("")
    lines.append(signal.summary)
    lines.append("")
    lines.append("⚠️ 以上为系统条件检查结果，不构成投资建议。最终决策由你做出。")
    return "\n".join(lines)
