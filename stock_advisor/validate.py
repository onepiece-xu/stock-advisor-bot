"""
代码改动后自动验证 —— 语法 / 配置 / 导入链 / 信号质量
Usage: python3 -B -m stock_advisor.validate --config config.yaml
"""
from __future__ import annotations

import sys
from pathlib import Path

def validate_syntax(package_dir: str = "stock_advisor") -> list[str]:
    """Compile-check all .py files in package."""
    errors = []
    for py_file in sorted(Path(package_dir).rglob("*.py")):
        if py_file.name.startswith("__"):
            continue
        try:
            import py_compile
            py_compile.compile(str(py_file), doraise=True)
        except py_compile.PyCompileError as e:
            errors.append(f"  ✗ {py_file}: {e}")
    return errors


def validate_imports() -> list[str]:
    """Verify all critical imports resolve."""
    errors = []
    modules = [
        "stock_advisor.analysis",
        "stock_advisor.review",
        "stock_advisor.runtime",
        "stock_advisor.trading_plan",
        "stock_advisor.state_builder",
        "stock_advisor.instruction_engine",
        "stock_advisor.multi_timeframe",
        "stock_advisor.multi_agent",
        "stock_advisor.opportunity_scanner",
        "stock_advisor.fund_flow",
        "stock_advisor.market_breadth",
        "stock_advisor.chrome_scraper",
        "stock_advisor.stop_loss",
        "stock_advisor.data_review",
        "stock_advisor.signal_tracker",
        "stock_advisor.threshold_optimizer",
        "stock_advisor.bridge_validator",
    ]
    for mod in modules:
        try:
            __import__(mod)
        except Exception as e:
            errors.append(f"  ✗ {mod}: {e}")
    return errors


def validate_signal_quality(db_path: str, lookback_days: int = 7) -> dict:
    """Check recent signal accuracy from stored data."""
    try:
        from .signal_tracker import evaluate_signal_accuracy
        stats = evaluate_signal_accuracy(days_lookback=lookback_days)
        return {
            "ok": True,
            "total": stats.get("total", 0),
            "buy_hit": stats.get("buy_hit_rate", 0),
            "sell_hit": stats.get("sell_hit_rate", 0),
            "overall": stats.get("overall_accuracy", 0),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def validate_trigger_consistency(trigger_path: str) -> list[str]:
    """Check triggers are well-formed."""
    import json
    errors = []
    try:
        raw = json.loads(Path(trigger_path).read_text(encoding="utf-8"))
        triggers = raw.get("triggers", [])
        required = ["code", "name", "action", "quantity", "priceMin", "priceMax"]
        for t in triggers:
            for field in required:
                if field not in t:
                    errors.append(f"  ✗ trigger {t.get('name','?')}: missing {field}")
            if t.get("action") not in ("buy", "sell", "hold"):
                errors.append(f"  ✗ trigger {t.get('name','?')}: invalid action {t.get('action')}")
            if float(t.get("priceMin", 0)) > float(t.get("priceMax", 0)):
                errors.append(f"  ✗ trigger {t.get('name','?')}: priceMin > priceMax")
    except Exception as e:
        errors.append(f"  ✗ trigger file read error: {e}")
    return errors


def run_validation(config_path: str, *, db_path: str | None = None, trigger_path: str | None = None) -> bool:
    """Run full validation suite. Returns True if all pass."""
    all_ok = True

    print("=" * 50)
    print("Stock Advisor — 代码验证")
    print("=" * 50)

    # 1. Syntax
    print("\n[1/5] 语法检查...")
    syntax_errors = validate_syntax()
    if syntax_errors:
        all_ok = False
        for e in syntax_errors:
            print(e)
    else:
        print("  ✓ 全部通过")

    # 2. Config
    print("\n[2/5] 配置加载...")
    try:
        from .config import require_valid_config
        config = require_valid_config(config_path)
        print(f"  ✓ 配置通过 (buy≥{config.monitor.decision_thresholds.buy_score} hold≥{config.monitor.decision_thresholds.hold_score})")
    except Exception as e:
        all_ok = False
        print(f"  ✗ {e}")
        return all_ok

    # 3. Imports
    print("\n[3/5] 模块导入...")
    import_errors = validate_imports()
    if import_errors:
        all_ok = False
        for e in import_errors:
            print(e)
    else:
        print("  ✓ 全部通过")

    # 4. Triggers
    if trigger_path:
        print("\n[4/5] 触发单一致性...")
        trigger_errors = validate_trigger_consistency(trigger_path)
        if trigger_errors:
            all_ok = False
            for e in trigger_errors:
                print(e)
        else:
            print("  ✓ 触发单格式正确")
    else:
        print("\n[4/5] 触发单检查... 跳过（无路径）")

    # 5. Signal quality
    if db_path:
        print("\n[5/5] 信号质量...")
        quality = validate_signal_quality(db_path)
        if quality.get("ok"):
            print(f"  ✓ 近7日 {quality['total']}条信号 | 买入命中{quality['buy_hit']:.0%} | 总体{quality['overall']:.0%}")
        else:
            print(f"  ⚠ {quality.get('error', 'unknown')}")
    else:
        print("\n[5/5] 信号质量... 跳过（无DB路径）")

    print("\n" + ("✓ 全部验证通过" if all_ok else "✗ 验证失败，请修复后重试"))
    print("=" * 50)
    return all_ok


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="代码改动后自动验证")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--db", help="SQLite行情库路径")
    parser.add_argument("--triggers", help="触发单JSON路径")
    args = parser.parse_args()

    ok = run_validation(
        args.config,
        db_path=args.db or "data/market.db",
        trigger_path=args.triggers or "data/trading_plan.json",
    )
    sys.exit(0 if ok else 1)
