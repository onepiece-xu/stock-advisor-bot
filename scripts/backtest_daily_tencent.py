#!/usr/bin/env python3
"""腾讯日线长周期策略回放 —— 校准评分阈值

用腾讯日线(前复权, 最多120根)复刻 analysis.py 的日线评分框架,
逐日打分生成信号, 统计信号后 1/3/5 日收益, 并网格扫描 buy/hold/reduce
阈值组合以最大化策略期望值。

用法:
    python -B scripts/backtest_daily_tencent.py --stocks sh601698 sz002439 --days 120
    python -B scripts/backtest_daily_tencent.py --scan   # 阈值网格扫描

数据源: https://web.ifzq.gtimg.cn/appstock/app/fqkline/get (腾讯, 与 Python SSL 栈兼容)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

BASE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{n},qfq"


def fetch_klines(symbol: str, days: int = 120) -> list[list]:
    """腾讯日线: [日期, 开, 收, 高, 低, 量]"""
    url = BASE.format(sym=symbol, n=days)
    r = subprocess.run(
        ["curl", "-s", "--max-time", "15", url], capture_output=True, text=True, timeout=30
    )
    if not r.stdout:
        raise RuntimeError(f"{symbol}: 拉取失败")
    d = json.loads(r.stdout)
    data = d.get("data", {}).get(symbol, {})
    key = "qfqday" if "qfqday" in data else ("day" if "day" in data else None)
    if not key:
        raise RuntimeError(f"{symbol}: 无K线数据 {list(data.keys())}")
    return data[key]


def sma(values: list[float], i: int, n: int) -> float:
    s = values[max(0, i - n + 1): i + 1]
    return sum(s) / len(s)


def rsi14(closes: list[float], i: int) -> float:
    if i < 15:
        return 50.0
    gains = losses = 0.0
    for j in range(i - 13, i + 1):
        d = closes[j] - closes[j - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    return 100.0 - 100.0 / (1.0 + gains / losses) if losses else 100.0


def score_daily(price: float, ma20: float, ma60: float, rsi: float, vol_ratio: float,
                change_pct: float) -> tuple[float, str]:
    """复刻 analysis.py 日线评分 (PHASE 1 + _decision_action 阈值)。返回 (分数, regime)。"""
    score = 50.0
    if price > ma20 > ma60:
        regime = "bull"
        score += 24
    elif price < ma20 < ma60:
        regime = "bear"
        score -= 30
    else:
        regime = "neutral"

    if ma20 > 0:
        bias = (price - ma20) / ma20 * 100.0
        if bias <= -5.0:
            score += 15 if regime == "bull" else -6
        elif bias <= -2.0:
            score += 6 if regime == "bull" else (3 if regime == "neutral" else 0)
        elif bias >= 5.0 and regime != "bull":
            score -= 18
        elif bias >= 3.0 and regime != "bull":
            score -= 9

    if rsi <= 25:
        score += 24
    elif rsi <= 32:
        score += 12
    elif rsi >= 80:
        score -= 30
    elif rsi >= 70:
        score -= 15

    if vol_ratio >= 2.0:
        score += 18 if change_pct >= 0 else -24
    elif vol_ratio <= 0.5:
        score += -12 if (change_pct >= 0 and regime != "bull") else (6 if change_pct < 0 else 0)
    elif (vol_ratio >= 1.2 and price > ma20 and change_pct > 0 and regime != "bear"):
        # v1.56.1 放量上攻（回测实证唯一正期望买点）
        score += 14

    return max(0.0, min(score, 100.0)), regime


def action_for(score: float, buy_t: float, hold_t: float, reduce_t: float) -> str:
    if score >= buy_t:
        return "buy"
    if score >= hold_t:
        return "hold"
    if score >= reduce_t:
        return "reduce"
    return "avoid"


def backtest(symbol: str, klines: list[list], buy_t: float, hold_t: float,
             reduce_t: float, verbose: bool = False) -> dict:
    closes = [float(k[1]) for k in klines]
    vols = [float(k[5]) for k in klines]
    n = len(closes)
    signals: list[dict] = []
    for i in range(20, n):
        price = closes[i]
        ma20 = sma(closes, i, 20)
        ma60 = sma(closes, i, 60)
        rsi = rsi14(closes, i)
        v5 = sma(vols, i, 5)
        v20 = sma(vols, i, 20)
        vol_ratio = v5 / v20 if v20 else 1.0
        change_pct = (closes[i] - closes[i - 1]) / closes[i - 1] * 100.0
        score, regime = score_daily(price, ma20, ma60, rsi, vol_ratio, change_pct)
        action = action_for(score, buy_t, hold_t, reduce_t)
        fwd = {}
        for horizon in (1, 3, 5):
            j = min(i + horizon, n - 1)
            fwd[horizon] = (closes[j] - price) / price * 100.0
        signals.append({
            "date": klines[i][0], "action": action, "score": score, "regime": regime,
            "fwd": fwd, "price": price,
        })
        if verbose and action in ("buy", "reduce"):
            print(f"  {symbol} {klines[i][0]} {action} 分{score:.0f} {regime} "
                  f"收盘{price:.2f} 5日{fwd[5]:+.2f}%")

    stats = defaultdict(lambda: {"cnt": 0, "fwd1": [], "fwd3": [], "fwd5": []})
    for s in signals:
        a = stats[s["action"]]
        a["cnt"] += 1
        a["fwd1"].append(s["fwd"][1])
        a["fwd3"].append(s["fwd"][3])
        a["fwd5"].append(s["fwd"][5])
    return {"symbol": symbol, "signals": signals, "stats": dict(stats)}


def fmt_stat(st: dict) -> str:
    if st["cnt"] == 0:
        return "无"
    def _l(xs):
        if not xs:
            return "-"
        win = sum(1 for x in xs if x > 0) / len(xs) * 100
        return f"{sum(xs)/len(xs):+.2f}%({win:.0f}%)"
    return f"n={st['cnt']} 1日{_l(st['fwd1'])} 3日{_l(st['fwd3'])} 5日{_l(st['fwd5'])}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", default="sh601698,sz002439,sh000300")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--buy", type=float, default=78.0)
    ap.add_argument("--hold", type=float, default=58.0)
    ap.add_argument("--reduce", type=float, default=38.0)
    ap.add_argument("--scan", action="store_true", help="网格扫描最优阈值")
    ap.add_argument("--probe", action="store_true", help="买入条件探针: 检验哪种候选买点历史上有正期望")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--quality-json", default="", help="导出每票每动作历史信号质量到 JSON (供收盘复盘标注用)")
    args = ap.parse_args()

    stocks = [s.strip() for s in args.stocks.split(",") if s.strip()]
    data = {}
    for sym in stocks:
        try:
            data[sym] = fetch_klines(sym, args.days)
            print(f"✓ {sym}: {len(data[sym])} 根日线 ({data[sym][0][0]} ~ {data[sym][-1][0]})")
        except Exception as exc:  # noqa: BLE001 —— 单只失败不阻塞整体
            print(f"✗ {sym}: {exc}")

    if not data:
        sys.exit(1)

    if args.scan:
        best = None
        for buy_t in (72, 75, 78, 82, 85):
            for hold_t in (50, 55, 58, 62, 65):
                for reduce_t in (35, 38, 42):
                    if hold_t >= buy_t or reduce_t >= hold_t:
                        continue
                    total_fwd5_buy = []
                    total_fwd5_reduce = []
                    for sym, kl in data.items():
                        r = backtest(sym, kl, buy_t, hold_t, reduce_t)
                        for s in r["signals"]:
                            if s["action"] == "buy":
                                total_fwd5_buy.append(s["fwd"][5])
                            elif s["action"] == "reduce":
                                total_fwd5_reduce.append(s["fwd"][5])
                    if not total_fwd5_buy:
                        continue
                    buy_avg = sum(total_fwd5_buy) / len(total_fwd5_buy)
                    buy_win = sum(1 for x in total_fwd5_buy if x > 0) / len(total_fwd5_buy) * 100
                    # 期望值: buy 盈利 + reduce 规避亏损(卖出后下跌=好)
                    red_avg = sum(total_fwd5_reduce) / len(total_fwd5_reduce) if total_fwd5_reduce else 0.0
                    ev = buy_avg - red_avg * 0.5
                    metric = (buy_win, ev, len(total_fwd5_buy))
                    if best is None or metric > best[0]:
                        best = (metric, (buy_t, hold_t, reduce_t))
        if best:
            (win, ev, cnt), (b, h, r_) = best
            print(f"\n🏆 最优阈值: buy≥{b:.0f} hold≥{h:.0f} reduce≥{r_:.0f}")
            print(f"   buy信号 {cnt} 个, 5日胜率 {win:.0f}%, 期望值 EV {ev:+.2f}%")
        return

    if args.probe:
        # ── 买入条件探针: 检验哪种"候选买点"历史上有正期望 ──
        conditions = {
            "A 趋势修复(价>MA20且MA20上行)": lambda p, ma20, ma20p, ma5, ma10, v20, hi20, lo20, mom20, rsi: p > ma20 and ma20 > ma20p,
            "B 动量修复(价>MA20且20日动量>3%)": lambda p, ma20, ma20p, ma5, ma10, v20, hi20, lo20, mom20, rsi: p > ma20 and mom20 > 3.0,
            "C 放量突破(价>前20日高且量比>1.5)": lambda p, ma20, ma20p, ma5, ma10, v20, hi20, lo20, mom20, rsi: p > hi20 and v20 > 1.5,
            "D 超卖(RSI<30)": lambda p, ma20, ma20p, ma5, ma10, v20, hi20, lo20, mom20, rsi: rsi < 30,
            "E 金叉(价>MA20且MA5>MA10)": lambda p, ma20, ma20p, ma5, ma10, v20, hi20, lo20, mom20, rsi: p > ma20 and ma5 > ma10,
            "F 放量上攻(价>MA20且量比>1.2且涨)": lambda p, ma20, ma20p, ma5, ma10, v20, hi20, lo20, mom20, rsi: p > ma20 and v20 > 1.2 and mom20 > 0,
            "G 趋势+突破(B且C)": lambda p, ma20, ma20p, ma5, ma10, v20, hi20, lo20, mom20, rsi: p > ma20 and mom20 > 3.0 and p > hi20,
            "H 超卖+次日阳(RSI<30且当日涨)": lambda p, ma20, ma20p, ma5, ma10, v20, hi20, lo20, mom20, rsi: rsi < 30 and mom20 > 0,
        }
        results = {k: {"cnt": 0, "fwd1": [], "fwd3": [], "fwd5": [], "fwd10": []} for k in conditions}
        for sym, kl in data.items():
            closes = [float(k[1]) for k in kl]
            vols = [float(k[5]) for k in kl]
            n = len(closes)
            for i in range(20, n - 10):
                p = closes[i]
                ma20 = sma(closes, i, 20)
                ma20p = sma(closes, i - 1, 20)
                ma5 = sma(closes, i, 5)
                ma10 = sma(closes, i, 10)
                v20 = (sma(vols, i, 5) / sma(vols, i, 20)) if sma(vols, i, 20) else 1.0
                hi20 = max(closes[max(0, i - 20): i])
                lo20 = min(closes[max(0, i - 20): i])
                mom20 = (p - closes[i - 20]) / closes[i - 20] * 100.0
                rsi = rsi14(closes, i)
                fwd = {h: (closes[min(i + h, n - 1)] - p) / p * 100.0 for h in (1, 3, 5, 10)}
                for name, cond in conditions.items():
                    try:
                        if cond(p, ma20, ma20p, ma5, ma10, v20, hi20, lo20, mom20, rsi):
                            st = results[name]
                            st["cnt"] += 1
                            for h in (1, 3, 5, 10):
                                st[f"fwd{h}"].append(fwd[h])
                    except Exception:
                        pass
        print(f"\n{'='*92}\n买入条件探针 (样本: {', '.join(data)} 120日)\n{'='*92}")
        print(f"{'条件':<26} {'n':>4} {'1日':>16} {'3日':>16} {'5日':>16} {'10日':>16}")
        for name, st in sorted(results.items(), key=lambda kv: (sum(kv[1]['fwd5']) / kv[1]['fwd5'].__len__()) if kv[1]['fwd5'] else -999, reverse=True):
            if st["cnt"] == 0:
                print(f"{name:<26} {'0':>4}  无信号")
                continue
            def _fmt(xs):
                avg = sum(xs) / len(xs)
                win = sum(1 for x in xs if x > 0) / len(xs) * 100
                return f"{avg:+.2f}%({win:.0f}%)"
            print(f"{name:<26} {st['cnt']:>4} {_fmt(st['fwd1']):>16} {_fmt(st['fwd3']):>16} {_fmt(st['fwd5']):>16} {_fmt(st['fwd10']):>16}")
        return

    print(f"\n{'='*78}\n阈值: buy≥{args.buy:.0f} hold≥{args.hold:.0f} reduce≥{args.reduce:.0f}  "
          f"(回放区间: 信号日~当日+5日)\n{'='*78}")
    quality: dict = {}
    for sym, kl in data.items():
        r = backtest(sym, kl, args.buy, args.hold, args.reduce, verbose=args.verbose)
        print(f"\n### {sym}")
        for action in ("buy", "hold", "reduce", "avoid"):
            st = r["stats"].get(action)
            if st:
                print(f"  {action:6s}: {fmt_stat(st)}")
        quality[sym] = {
            act: {"n": st["cnt"],
                  "ret1": round(sum(st["fwd1"]) / len(st["fwd1"]), 2) if st["fwd1"] else 0.0,
                  "ret3": round(sum(st["fwd3"]) / len(st["fwd3"]), 2) if st["fwd3"] else 0.0,
                  "ret5": round(sum(st["fwd5"]) / len(st["fwd5"]), 2) if st["fwd5"] else 0.0,
                  "win1": round(sum(1 for x in st["fwd1"] if x > 0) / len(st["fwd1"]) * 100) if st["fwd1"] else 0,
                  "win3": round(sum(1 for x in st["fwd3"] if x > 0) / len(st["fwd3"]) * 100) if st["fwd3"] else 0,
                  "win5": round(sum(1 for x in st["fwd5"] if x > 0) / len(st["fwd5"]) * 100) if st["fwd5"] else 0}
            for act, st in r["stats"].items()
        }
    if args.quality_json:
        with open(args.quality_json, "w", encoding="utf-8") as f:
            json.dump(quality, f, ensure_ascii=False, indent=1)
        print(f"\n✓ 信号质量已导出: {args.quality_json}")


if __name__ == "__main__":
    main()
