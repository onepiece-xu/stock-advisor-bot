"""
数据源健康检查 —— 统一容错层
集中检测所有外部API可用性，避免各模块各自try/except

Usage:
    from .data_health import check_all, DataSourceStatus
    status = check_all()
    if not status.tencent_quote_ok:
        logger.warning("腾讯行情API不可用")
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DataSourceStatus:
    tencent_quote_ok: bool = False       # 腾讯实时行情 (qt.gtimg.cn)
    tencent_kline_ok: bool = False       # 腾讯日线K线 (web.ifzq.gtimg.cn)
    tencent_week_ok: bool = False        # 腾讯周线K线
    eastmoney_northbound_ok: bool = False  # 东方财富北向资金 (akshare)
    chrome_cdp_ok: bool = False          # Chrome CDP (主力资金流)
    failures: list[str] = field(default_factory=list)
    checked_at: float = 0.0

    @property
    def all_ok(self) -> bool:
        return all([
            self.tencent_quote_ok,
            self.tencent_kline_ok,
            self.tencent_week_ok,
            self.eastmoney_northbound_ok,
            self.chrome_cdp_ok,
        ])

    @property
    def degraded(self) -> bool:
        """At least core quote source is available."""
        return self.tencent_quote_ok and self.tencent_kline_ok


def _curl_get(url: str, timeout: int = 5) -> tuple[bool, str]:
    """HTTP check via requests (cross-platform; replaces curl)."""
    from .platform_compat import http_get_text
    text = http_get_text(url, timeout=timeout + 2)
    if len(text.strip()) > 10:
        return True, text[:200]
    return False, "empty response"


def _python_get(url: str, timeout: int = 5) -> tuple[bool, str]:
    """Python requests check (works for Tencent APIs)."""
    try:
        import requests
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        resp.raise_for_status()
        return True, resp.text[:200]
    except Exception as e:
        return False, str(e)[:100]


def check_tencent_quote() -> tuple[bool, str]:
    ok, resp = _python_get("http://qt.gtimg.cn/q=sh601698", timeout=5)
    if ok and "601698" in resp:
        return True, "OK"
    return False, resp


def check_tencent_kline() -> tuple[bool, str]:
    ok, resp = _python_get(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh601698,day,,,5,qfq",
        timeout=5,
    )
    if ok and "qfqday" in resp:
        return True, "OK"
    return False, resp


def check_tencent_week() -> tuple[bool, str]:
    ok, resp = _python_get(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh601698,week,,,5,qfq",
        timeout=5,
    )
    if ok and "qfqweek" in resp:
        return True, "OK"
    return False, resp


def check_eastmoney_northbound() -> tuple[bool, str]:
    try:
        import akshare as ak
        df = ak.stock_hsgt_north_net_flow_in_em(symbol="北上")
        if df is not None and len(df) > 0:
            return True, f"OK ({len(df)} rows)"
        return False, "empty dataframe"
    except Exception as e:
        return False, str(e)[:100]


def check_chrome_cdp() -> tuple[bool, str]:
    try:
        from .platform_compat import http_get_text
        text = http_get_text("http://172.27.144.1:9222/json/version", timeout=5)
        if "Browser" in text:
            return True, "OK"
        return False, "no Browser in response"
    except Exception as e:
        return False, str(e)[:100]


def check_all() -> DataSourceStatus:
    status = DataSourceStatus(checked_at=time.time())

    ok, msg = check_tencent_quote()
    status.tencent_quote_ok = ok
    if not ok:
        status.failures.append(f"腾讯行情: {msg}")

    ok, msg = check_tencent_kline()
    status.tencent_kline_ok = ok
    if not ok:
        status.failures.append(f"腾讯K线: {msg}")

    ok, msg = check_tencent_week()
    status.tencent_week_ok = ok
    if not ok:
        status.failures.append(f"腾讯周线: {msg}")

    ok, msg = check_eastmoney_northbound()
    status.eastmoney_northbound_ok = ok
    if not ok:
        status.failures.append(f"北向资金: {msg}")

    ok, msg = check_chrome_cdp()
    status.chrome_cdp_ok = ok
    if not ok:
        status.failures.append(f"Chrome CDP: {msg}")

    return status


def health_report(status: DataSourceStatus | None = None) -> str:
    """Human-readable health report."""
    if status is None:
        status = check_all()

    lines = ["📡 数据源健康检查", f"  时间: {time.strftime('%H:%M:%S')}", ""]
    checks = [
        ("腾讯实时行情", status.tencent_quote_ok),
        ("腾讯日线K线", status.tencent_kline_ok),
        ("腾讯周线K线", status.tencent_week_ok),
        ("北向资金", status.eastmoney_northbound_ok),
        ("Chrome CDP", status.chrome_cdp_ok),
    ]
    for name, ok in checks:
        icon = "✓" if ok else "✗"
        lines.append(f"  {icon} {name}")

    if status.failures:
        lines.append(f"\n  ⚠ {len(status.failures)} 项不可用:")
        for f in status.failures:
            lines.append(f"    - {f}")

    if status.degraded:
        lines.append(f"\n  ⚡ 核心数据源可用，降级运行")
    elif status.all_ok:
        lines.append(f"\n  ✅ 全部正常")

    return "\n".join(lines)
