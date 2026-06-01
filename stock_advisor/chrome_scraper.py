#!/usr/bin/env python3
"""Chrome CDP 抓取东方财富个股资金流 + 市场涨跌家数。

原理：东方财富封了服务器IP，但浏览器IP可以访问。
通过本地 Chrome DevTools Protocol 用 JSONP 方式注入请求，
绕开IP封锁拿到数据。

依赖: websocket-client (pip install websocket-client)
需要: Windows 宿主机 Chrome 开启远程调试 (--remote-debugging-port=9222)

Usage:
  python3 -B stock_advisor/chrome_scraper.py
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import date
from decimal import Decimal
from typing import Optional

from websocket import create_connection as ws_connect, WebSocket

logger = logging.getLogger(__name__)

CDP_HOST = "http://172.27.144.1:9222"
CACHE_TTL = 120  # 2分钟缓存

_cdp_available: bool | None = None  # None=未检测, True=可用, False=不可用

_fund_flow_cache: dict[str, tuple[float, dict]] = {}
_breadth_cache: tuple[float, dict] = (0, {})


def _is_cache_valid(ts: float, ttl: float = CACHE_TTL) -> bool:
    return ts > 0 and (time.time() - ts) < ttl


def _cdp_call(ws: WebSocket, method: str, params: Optional[dict] = None) -> dict:
    """发送 CDP 命令并等待响应。"""
    msg = json.dumps({"id": 1, "method": method, "params": params or {}})
    ws.send(msg)
    while True:
        raw = ws.recv()
        resp = json.loads(raw)
        if resp.get("id") == 1:
            return resp


def _get_ws(page_url: str = None) -> WebSocket:
    """获取 Chrome CDP websocket 连接。"""
    resp = urllib.request.urlopen(f"{CDP_HOST}/json")
    pages = json.loads(resp.read())

    if page_url:
        target = next((p for p in pages if page_url in p.get("url", "")), None)
    else:
        target = pages[0] if pages else None

    if not target:
        raise RuntimeError("No Chrome page available for CDP")

    ws = ws_connect(target["webSocketDebuggerUrl"], timeout=10)
    _cdp_call(ws, "Runtime.enable")
    return ws


def _fetch_jsonp(ws: WebSocket, url: str, timeout: float = 8.0) -> dict:
    """在 Chrome 页面中执行 JSONP 请求。"""
    js = f"""
    (() => {{
        return new Promise((resolve, reject) => {{
            const cb = 'hc' + Date.now() + Math.random().toString(36).slice(2);
            window[cb] = function(data) {{
                delete window[cb];
                try {{ document.head.removeChild(script); }} catch(e) {{}}
                resolve(JSON.stringify(data));
            }};
            const script = document.createElement('script');
            script.src = '{url}&cb=' + cb;
            script.onerror = () => reject(new Error('jsonp_load_failed'));
            document.head.appendChild(script);
            setTimeout(() => reject(new Error('jsonp_timeout')), {int(timeout * 1000)});
        }});
    }})()
    """
    result = _cdp_call(ws, "Runtime.evaluate", {
        "expression": js,
        "awaitPromise": True,
        "returnByValue": True,
    })
    value = result.get("result", {}).get("result", {}).get("value", "")
    if not value:
        raise RuntimeError("CDP evaluate returned no value")
    return json.loads(value)


# ═══════════════════════════════════════════════════════════════
# 个股资金流
# ═══════════════════════════════════════════════════════════════

FUND_FLOW_URL = (
    "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    "?lmt=1&klt=101"
    "&fields1=f1,f2,f3,f7"
    "&fields2=f51,f52,f53,f54,f55,f56"
    "&ut=b2884a393a59ad64002292a3e90d46a5"
    "&secid={secid}"
)

FUND_FLOW_MINUTE_URL = (
    "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
    "?lmt=20&klt=1"
    "&fields1=f1,f2,f3,f7"
    "&fields2=f51,f52,f53,f54,f55,f56"
    "&ut=b2884a393a59ad64002292a3e90d46a5"
    "&secid={secid}"
)

CACHE_TTL = 120  # 日级别缓存2分钟
CACHE_TTL_RT = 60  # 实时缓存1分钟
def get_stock_fund_flow(code: str, force_refresh: bool = False) -> Optional[dict]:
    """获取个股今日主力资金流向。

    Args:
        code: 股票代码，如 '601698'

    Returns:
        {code, name, date, main_net_yi, super_large_yi, large_yi, medium_yi, small_yi}
        main_net_yi > 0 表示主力净流入
        失败返回 None
    """
    global _fund_flow_cache

    if not force_refresh and code in _fund_flow_cache:
        ts, data = _fund_flow_cache[code]
        if _is_cache_valid(ts):
            return data

    secid = f"{'1' if code.startswith(('6', '9')) else '0'}.{code}"
    url = FUND_FLOW_URL.format(secid=secid)

    ws = None
    try:
        ws = _get_ws()
        data = _fetch_jsonp(ws, url, timeout=8)
        klines = data.get("data", {}).get("klines", [])
        if not klines:
            return None

        parts = klines[-1].split(",")
        name = data.get("data", {}).get("name", code)

        result = {
            "code": code,
            "name": name,
            "date": parts[0],
            # f52=主力净流入(元), f53=超大单, f54=大单, f55=中单, f56=小单
            "main_net_yi": Decimal(str(float(parts[1]) / 1e8)).quantize(Decimal("0.01")),
            "super_large_yi": Decimal(str(float(parts[2]) / 1e8)).quantize(Decimal("0.01")),
            "large_yi": Decimal(str(float(parts[3]) / 1e8)).quantize(Decimal("0.01")),
            "medium_yi": Decimal(str(float(parts[4]) / 1e8)).quantize(Decimal("0.01")),
            "small_yi": Decimal(str(float(parts[5]) / 1e8)).quantize(Decimal("0.01")),
        }
        _fund_flow_cache[code] = (time.time(), result)
        return result
    except Exception as e:
        global _cdp_available
        _cdp_available = False
        logger.warning(f"Chrome CDP 资金流获取失败 ({code}): {e}")
        return None
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass


def get_multi_fund_flow(codes: list[str]) -> dict[str, dict]:
    """批量获取个股资金流。"""
    # CDP 不支持真正的并行，串行拉取
    result = {}
    for code in codes:
        ff = get_stock_fund_flow(code)
        if ff:
            result[code] = ff
    return result


def get_stock_fund_flow_realtime(code: str, force_refresh: bool = False) -> Optional[dict]:
    """获取个股盘中实时资金流（分钟级）。

    Returns:
        {
            code, name, date,
            cumulative_yi: Decimal,  # 今日累计主力净额(亿)
            direction: str,           # 🟢/🔴/⚪
            minutes: [               # 最近5分钟明细
                {time: "14:55", delta_yi: Decimal},
                ...
            ]
        }
    """
    global _fund_flow_cache
    cache_key = f"{code}_rt"

    if not force_refresh and cache_key in _fund_flow_cache:
        ts, data = _fund_flow_cache[cache_key]
        if _is_cache_valid(ts, ttl=CACHE_TTL_RT):
            return data

    secid = f"{'1' if code.startswith(('6', '9')) else '0'}.{code}"
    url = FUND_FLOW_MINUTE_URL.format(secid=secid)

    ws = None
    try:
        ws = _get_ws()
        data = _fetch_jsonp(ws, url, timeout=8)
        klines = data.get("data", {}).get("klines", [])
        if not klines:
            return None

        name = data.get("data", {}).get("name", code)

        # 解析分钟数据，取最后5条
        recent = klines[-5:]
        minutes = []
        prev_main = None
        for k in recent:
            parts = k.split(",")
            time_str = parts[0].split(" ")[-1][:5] if " " in parts[0] else parts[0][-5:]
            main_val = float(parts[1])

            delta = Decimal("0")
            if prev_main is not None:
                delta = Decimal(str((main_val - prev_main) / 1e8)).quantize(Decimal("0.01"))
            prev_main = main_val

            minutes.append({
                "time": time_str,
                "cumulative_yi": Decimal(str(main_val / 1e8)).quantize(Decimal("0.01")),
                "delta_yi": delta,
            })

        cumulative = Decimal(str(float(klines[-1].split(",")[1]) / 1e8)).quantize(Decimal("0.01"))
        direction = "🟢" if cumulative > 0 else ("🔴" if cumulative < 0 else "⚪")

        result = {
            "code": code,
            "name": name,
            "date": klines[-1].split(",")[0].split(" ")[0],
            "cumulative_yi": cumulative,
            "direction": direction,
            "minutes": minutes,
        }
        _fund_flow_cache[cache_key] = (time.time(), result)
        return result
    except Exception as e:
        logger.warning(f"Chrome CDP 实时资金流失败 ({code}): {e}")
        return None
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════
# 涨跌家数
# ═══════════════════════════════════════════════════════════════

BREADTH_URL = (
    "https://push2.eastmoney.com/api/qt/ulist.np/get"
    "?fltt=2"
    "&secids=1.000001,0.399001"
    "&fields=f2,f3,f4,f12,f104,f105,f106"
    "&ut=b2884a393a59ad64002292a3e90d46a5"
)


def get_market_breadth_cdp(force_refresh: bool = False) -> dict:
    """通过 CDP 获取沪深涨跌家数。

    Returns:
        {sh_up, sh_down, sh_flat, sz_up, sz_down, sz_flat, total_up, total_down, up_ratio}
    """
    global _breadth_cache
    if not force_refresh and _is_cache_valid(_breadth_cache[0]):
        return _breadth_cache[1]

    ws = None
    try:
        ws = _get_ws()
        data = _fetch_jsonp(ws, BREADTH_URL, timeout=8)
        items = data.get("data", {}).get("diff", [])

        sh_up = sh_down = sh_flat = 0
        sz_up = sz_down = sz_flat = 0

        for it in items:
            code = it.get("f12", "")
            up = int(it.get("f104", 0))
            down = int(it.get("f105", 0))
            flat = int(it.get("f106", 0))
            if code == "000001":
                sh_up, sh_down, sh_flat = up, down, flat
            elif code == "399001":
                sz_up, sz_down, sz_flat = up, down, flat

        total_up = sh_up + sz_up
        total_down = sh_down + sz_down
        total = total_up + total_down + sh_flat + sz_flat
        up_ratio = Decimal(str(total_up)) / Decimal(str(max(total, 1))) * 100

        result = {
            "sh_up": sh_up, "sh_down": sh_down, "sh_flat": sh_flat,
            "sz_up": sz_up, "sz_down": sz_down, "sz_flat": sz_flat,
            "total_up": total_up, "total_down": total_down,
            "up_ratio": up_ratio.quantize(Decimal("0.1")),
        }
        _breadth_cache = (time.time(), result)
        return result
    except Exception as e:
        logger.warning(f"Chrome CDP 涨跌家数失败: {e}")
        return {}
    finally:
        if ws:
            try:
                ws.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=== 涨跌家数 ===")
    b = get_market_breadth_cdp(force_refresh=True)
    if b:
        print(f"上证: ↑{b['sh_up']} ↓{b['sh_down']} ={b['sh_flat']}")
        print(f"深证: ↑{b['sz_up']} ↓{b['sz_down']} ={b['sz_flat']}")
        print(f"全市场: ↑{b['total_up']} ↓{b['total_down']} 上涨比{b['up_ratio']}%")
    else:
        print("获取失败")

    print("\n=== 个股资金流 ===")
    for code in ["601698", "000063", "002439"]:
        ff = get_stock_fund_flow(code, force_refresh=True)
        if ff:
            d = "🟢流入" if ff["main_net_yi"] > 0 else ("🔴流出" if ff["main_net_yi"] < 0 else "⚪持平")
            print(f"\n{ff['name']}({code}) {ff['date']}:")
            print(f"  主力净额: {d} {abs(ff['main_net_yi']):.2f}亿")
            print(f"  超大单:{ff['super_large_yi']:+.2f} 大单:{ff['large_yi']:+.2f}")
            print(f"  中单:{ff['medium_yi']:+.2f} 小单:{ff['small_yi']:+.2f}")
        else:
            print(f"\\n{code}: 获取失败")


def cdp_available() -> bool | None:
    """Check if Chrome CDP is reachable. None=never tried, True=available, False=unavailable."""
    global _cdp_available
    if _cdp_available is not None:
        return _cdp_available
    try:
        import urllib.request
        resp = urllib.request.urlopen(f"{CDP_HOST}/json/version", timeout=3)
        data = json.loads(resp.read())
        _cdp_available = "Browser" in data.get("Browser", "")
        return _cdp_available
    except Exception:
        _cdp_available = False
        return False
