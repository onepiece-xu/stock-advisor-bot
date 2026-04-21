from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import requests

from .analysis import analyze_quotes
from .backtest import (
    optimize_decision_thresholds,
    render_minute_backtest,
    render_optimization_report,
    run_minute_backtest,
)
from .briefing import format_mobile_digest, format_mobile_replay, format_mobile_signal
from .config import AppConfig
from .habit_learning import build_trading_habit_profile, render_trading_habit_profile
from .historical import (
    analyze_historical_point,
    compare_historical_points,
    render_historical_advice,
    render_historical_compare,
)
from .logging_utils import get_logger
from .market_hours import is_high_volatility_period
from .market_overview import build_market_overview, render_market_overview
from .models import StockQuote, StockRef
from .portfolio import find_holding, load_snapshot as load_portfolio_snapshot
from .providers import EastmoneyMarketSnapshotProvider, EastmoneyMinuteHistoryProvider, TencentQuoteProvider
from .review import build_close_review
from .storage import cache_quotes, connect_db, fetch_latest_briefing, load_recent_quotes, replay_signal_stats


SUPPORTED_ACTIONS = {"buy", "hold", "reduce", "avoid"}
SUPPORTED_LEVELS = {"ALERT", "INFO", "NEUTRAL"}
MENTION_PATTERNS = [
    re.compile(r"<at[^>]*>.*?</at>", re.IGNORECASE),
    re.compile(r"@_user_\d+"),
]
logger = get_logger(__name__)


@dataclass(slots=True)
class ParsedCommand:
    name: str
    args: list[str]


class FeishuBotClient:
    def __init__(self, app_id: str, app_secret: str) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self._tenant_token: str | None = None
        self._tenant_token_expire_at = 0.0

    def send_text_to_chat(self, chat_id: str, text: str) -> None:
        for chunk in _chunk_text(text):
            self._request(
                "POST",
                "https://open.feishu.cn/open-apis/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                json_body={
                    "receive_id": chat_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": chunk}, ensure_ascii=False),
                },
            )

    def _request(self, method: str, url: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._get_tenant_access_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }
        response = requests.request(method, url, headers=headers, params=params, json=json_body, timeout=12)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") not in (0, None):
            raise RuntimeError(f"Feishu API error: {payload}")
        return payload

    def _get_tenant_access_token(self) -> str:
        if self._tenant_token and time.time() < self._tenant_token_expire_at:
            return self._tenant_token

        response = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=12,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"Feishu tenant_access_token error: {payload}")
        self._tenant_token = str(payload["tenant_access_token"])
        expires_in = int(payload.get("expire", 7200))
        self._tenant_token_expire_at = time.time() + max(expires_in - 60, 60)
        return self._tenant_token


def serve_feishu_bot(config: AppConfig) -> None:
    if not config.feishu_bot.enabled:
        raise RuntimeError("feishu_bot.enabled=false, refusing to start bot server")
    if not config.feishu_bot.app_id or not config.feishu_bot.app_secret:
        raise RuntimeError("feishu_bot.app_id/app_secret are required")

    client = FeishuBotClient(config.feishu_bot.app_id, config.feishu_bot.app_secret)

    class FeishuBotHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            try:
                raw_body = self.rfile.read(int(self.headers.get("Content-Length", "0") or "0"))
                payload = json.loads(raw_body.decode("utf-8") or "{}")
                if payload.get("type") == "url_verification":
                    self._handle_url_verification(payload)
                    return
                if payload.get("encrypt"):
                    logger.warning("Rejected encrypted Feishu callback because encryption support is not implemented")
                    self._send_json(
                        {
                            "code": 1,
                            "msg": "encrypted callback is not supported; disable event encryption first",
                        },
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return

                if not _is_valid_verification_token(config, payload):
                    self._send_json({"code": 1, "msg": "invalid verification token"}, status=HTTPStatus.FORBIDDEN)
                    return

                _handle_feishu_event(config, client, payload)
                self._send_json({"code": 0, "msg": "ok"})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"code": 1, "msg": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _handle_url_verification(self, payload: dict[str, Any]) -> None:
            if not _is_valid_verification_token(config, payload):
                self._send_json({"code": 1, "msg": "invalid verification token"}, status=HTTPStatus.FORBIDDEN)
                return
            self._send_json({"challenge": payload.get("challenge", "")})

        def _send_json(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((config.feishu_bot.listen_host, config.feishu_bot.listen_port), FeishuBotHandler)
    print(f"[feishu-bot] listening on http://{config.feishu_bot.listen_host}:{config.feishu_bot.listen_port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _handle_feishu_event(config: AppConfig, client: FeishuBotClient, payload: dict[str, Any]) -> None:
    header = payload.get("header", {})
    if header.get("event_type") != "im.message.receive_v1":
        return

    event = payload.get("event", {})
    message = event.get("message", {})
    sender = event.get("sender", {})
    if sender.get("sender_type") == "app":
        return
    if message.get("message_type") != "text":
        return

    chat_id = str(message.get("chat_id", ""))
    if config.feishu_bot.allowed_chat_ids and chat_id not in config.feishu_bot.allowed_chat_ids:
        client.send_text_to_chat(chat_id, "当前会话未加入允许名单，已忽略。")
        return

    command_text = _extract_command_text(message.get("content", ""))
    response_text = run_feishu_command(config, command_text)
    if response_text:
        client.send_text_to_chat(chat_id, response_text)


def run_feishu_command(config: AppConfig, command_text: str) -> str:
    command = _parse_command(command_text)
    if command.name in {"help", "h", "?"}:
        return _help_text()
    if command.name == "brief":
        conn = connect_db(config.storage.sqlite_path)
        return format_mobile_digest(fetch_latest_briefing(conn))
    if command.name == "review":
        artifact = build_close_review(config)
        return artifact.body
    if command.name == "habit":
        conn = connect_db(config.storage.sqlite_path)
        return render_trading_habit_profile(build_trading_habit_profile(conn), mobile=True)
    if command.name == "market":
        return render_market_overview(build_market_overview(config), mobile=True)
    if command.name == "quote":
        if not command.args:
            return "用法: quote 601698"
        conn = connect_db(config.storage.sqlite_path)
        return _render_latest_quote(fetch_latest_briefing(conn), command.args[0])
    if command.name == "scan":
        if not command.args:
            return "用法: scan 601698"
        return _scan_live_symbol(config, command.args[0])
    if command.name == "at":
        datetimes, stock_tokens = _extract_history_datetimes(command.args)
        if len(datetimes) != 1:
            return "用法: at 2026-04-17 14:20 [601698] 或 at 601698 2026-04-17 14:20"
        requested_at = datetimes[0]
        stocks = [_resolve_stock_ref(config, token) for token in stock_tokens] if stock_tokens else None
        items = analyze_historical_point(config, requested_at, stocks=stocks)
        return render_historical_advice(items, mobile=True)
    if command.name == "compare":
        datetimes, stock_tokens = _extract_history_datetimes(command.args)
        if len(datetimes) != 2:
            return "用法: compare 2026-04-17 14:20 2026-04-17 15:00 [601698]"
        start_at, end_at = sorted(datetimes)
        stocks = [_resolve_stock_ref(config, token) for token in stock_tokens] if stock_tokens else None
        items = compare_historical_points(config, start_at, end_at, stocks=stocks)
        return render_historical_compare(items, mobile=True)
    if command.name == "backtest":
        days, stock_tokens = _parse_backtest_args(command.args)
        stocks = [_resolve_stock_ref(config, token) for token in stock_tokens] if stock_tokens else None
        stats = run_minute_backtest(config, symbols=stocks, ndays=days)
        return render_minute_backtest(stats, mobile=True)
    if command.name == "optimize":
        days, stock_tokens = _parse_backtest_args(command.args)
        stocks = [_resolve_stock_ref(config, token) for token in stock_tokens] if stock_tokens else None
        report = optimize_decision_thresholds(config, symbols=stocks, ndays=days)
        return render_optimization_report(report, mobile=True)
    if command.name == "replay":
        conn = connect_db(config.storage.sqlite_path)
        filters = _parse_replay_filters(config, command.args)
        stats = replay_signal_stats(conn, **filters)
        return format_mobile_replay(stats, symbol=filters["symbol"], level=filters["signal_level"], action=filters["action"])
    return _help_text(prefix=f"未识别命令: {command.name}")


def _render_latest_quote(items: list[dict], query: str) -> str:
    normalized = _normalize_symbol(query)
    for item in items:
        if query == item["code"] or normalized == item["symbol"] or query in item["name"]:
            lines = [
                f"【{item['code']} {item['name']}】",
                f"现价: {item['current_price']}",
                f"涨跌幅: {_signed(item['change_percent'])}%",
                f"动作: {item['action']}",
                f"评分: {item['score'] if item['score'] is not None else '-'}",
                f"状态: {item['regime']} / {item['confidence']}",
                f"信号: {item['signal_level']}",
                f"理由: {'；'.join(item['rationale'][:2]) if item['rationale'] else '暂无明显理由'}",
            ]
            if item["risk_flags"]:
                lines.append(f"风险: {'；'.join(item['risk_flags'][:2])}")
            lines.append("仅供参考，不构成投资建议")
            return "\n".join(lines)
    return f"未找到 {query} 的最新缓存，请先运行 monitor-once 或确认代码是否在观察列表中。"


def _scan_live_symbol(config: AppConfig, query: str) -> str:
    stock = _resolve_stock_ref(config, query)
    provider = _build_provider(config)
    conn = connect_db(config.storage.sqlite_path)
    history = _load_stock_history(config, conn, provider, stock)
    if not history:
        raise RuntimeError(f"未拉到 {stock.code} 的实时窗口数据")
    advance_ratio, rank_map, sector_boards = _fetch_market_context(config)
    portfolio_snapshot = _load_portfolio_snapshot(config)
    holding = find_holding(portfolio_snapshot, stock.code)
    result = analyze_quotes(
        history,
        config.monitor,
        portfolio_holding=holding,
        benchmark_history=_load_benchmark_history(config),
        trading_habit_profile=build_trading_habit_profile(conn),
        market_advance_ratio=advance_ratio,
        hot_stock_rank=rank_map.get(stock.code, 0),
        is_volatile_period=is_high_volatility_period(),
        portfolio_cash_ratio=_compute_cash_ratio(portfolio_snapshot),
        sector_boards=sector_boards,
        portfolio_position_ratio=_compute_position_ratio(portfolio_snapshot, holding, history[-1].current_price),
    )
    return format_mobile_signal(result.title, result.message)


def _parse_replay_filters(config: AppConfig, args: list[str]) -> dict[str, str | None]:
    filters: dict[str, str | None] = {"symbol": None, "signal_level": None, "action": None}
    for token in args:
        if "=" in token:
            key, value = token.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if key == "symbol":
                filters["symbol"] = _resolve_stock_ref(config, value).symbol
            elif key == "level":
                filters["signal_level"] = value.upper()
            elif key == "action":
                filters["action"] = value
            continue
        upper = token.upper()
        if upper in SUPPORTED_LEVELS:
            filters["signal_level"] = upper
        elif token in SUPPORTED_ACTIONS:
            filters["action"] = token
        else:
            filters["symbol"] = _resolve_stock_ref(config, token).symbol
    return filters


def _resolve_stock_ref(config: AppConfig, query: str) -> StockRef:
    normalized = _normalize_symbol(query)
    for stock in config.monitor.stocks:
        if stock.symbol == normalized or stock.code == query:
            return stock
    if re.fullmatch(r"\d{6}", query):
        exchange = "sh" if query.startswith(("5", "6", "9")) else "sz"
        return StockRef(exchange=exchange, code=query)
    raise RuntimeError(f"无法识别股票代码: {query}")


def _normalize_symbol(query: str) -> str:
    query = query.strip().lower()
    if re.fullmatch(r"(sh|sz)\d{6}", query):
        return query
    return query


def _extract_command_text(raw_content: str) -> str:
    try:
        payload = json.loads(raw_content)
        text = str(payload.get("text", ""))
    except json.JSONDecodeError:
        text = raw_content
    for pattern in MENTION_PATTERNS:
        text = pattern.sub(" ", text)
    return " ".join(text.split()).strip()


def _parse_command(text: str) -> ParsedCommand:
    normalized = text.strip()
    if not normalized:
        return ParsedCommand(name="help", args=[])
    tokens = normalized.split()
    return ParsedCommand(name=tokens[0].lower(), args=tokens[1:])


def _is_valid_verification_token(config: AppConfig, payload: dict[str, Any]) -> bool:
    expected = config.feishu_bot.verification_token
    if not expected:
        return True
    actual = payload.get("token") or payload.get("header", {}).get("token") or payload.get("event", {}).get("token")
    return actual == expected


def _help_text(*, prefix: str | None = None) -> str:
    lines = []
    if prefix:
        lines.append(prefix)
    lines.extend(
        [
            "支持命令:",
            "help",
            "brief",
            "review",
            "habit",
            "market",
            "quote 601698",
            "scan 601698",
            "at 2026-04-17 14:20",
            "at 2026-04-17 14:20 601698",
            "at 601698 2026-04-17 14:20",
            "compare 2026-04-17 14:20 2026-04-17 15:00",
            "compare 601698 2026-04-17 14:20 2026-04-17 15:00",
            "backtest",
            "backtest 3",
            "backtest 601698",
            "backtest 3 601698",
            "optimize",
            "optimize 3",
            "optimize 601698",
            "optimize 3 601698",
            "market",
            "replay",
            "replay reduce",
            "replay ALERT",
            "replay 601698",
            "replay action=reduce level=ALERT symbol=601698",
        ]
    )
    return "\n".join(lines)


def _signed(value: float | None) -> str:
    if value is None:
        return "-"
    return f"+{value:.2f}" if value > 0 else f"{value:.2f}"


def _chunk_text(text: str, limit: int = 1800) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = line
    if current:
        chunks.append(current)
    return chunks or [text]


def _parse_history_datetime(text: str) -> datetime:
    normalized = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    raise RuntimeError(f"无法解析历史时点: {text}")


def _extract_history_datetimes(args: list[str]) -> tuple[list[datetime], list[str]]:
    datetimes: list[datetime] = []
    remaining: list[str] = []
    index = 0
    while index < len(args):
        one_token = _try_parse_history_datetime(args[index])
        if one_token is not None:
            datetimes.append(one_token)
            index += 1
            continue
        if index + 1 < len(args):
            two_tokens = _try_parse_history_datetime(f"{args[index]} {args[index + 1]}")
            if two_tokens is not None:
                datetimes.append(two_tokens)
                index += 2
                continue
        remaining.append(args[index])
        index += 1
    return datetimes, remaining


def _try_parse_history_datetime(text: str) -> datetime | None:
    try:
        return _parse_history_datetime(text)
    except RuntimeError:
        return None


def _parse_backtest_args(args: list[str]) -> tuple[int, list[str]]:
    days = 5
    remaining: list[str] = []
    for token in args:
        if token.isdigit() and len(token) <= 2:
            days = max(1, min(int(token), 10))
            continue
        remaining.append(token)
    return days, remaining


def _compute_cash_ratio(snapshot) -> Decimal | None:
    if snapshot is None or snapshot.total_assets <= 0:
        return None
    return (snapshot.cash / snapshot.total_assets).quantize(Decimal("0.0001"))


def _compute_position_ratio(snapshot, holding, current_price: Decimal) -> Decimal | None:
    if snapshot is None or snapshot.total_assets <= 0 or holding is None or holding.quantity <= 0:
        return None
    position_value = Decimal(str(holding.quantity)) * current_price
    return (position_value / snapshot.total_assets).quantize(Decimal("0.0001"))


def _fetch_market_context(config: AppConfig) -> tuple[Decimal, dict[str, int], list[dict]]:
    advance_ratio = Decimal("0")
    rank_map: dict[str, int] = {}
    sector_boards: list[dict] = []
    try:
        provider = EastmoneyMarketSnapshotProvider(config.monitor)
        breadth = provider.fetch_market_breadth()
        total = breadth.get("up_count", 0) + breadth.get("flat_count", 0) + breadth.get("down_count", 0)
        if total > 0:
            advance_ratio = Decimal(str(breadth["up_count"])) / Decimal(str(total))
        top_stocks = provider.fetch_top_stocks(limit=50)
        rank_map = {item["code"]: idx + 1 for idx, item in enumerate(top_stocks)}
        sector_boards = provider.fetch_sector_boards(kind="industry", limit=5) + provider.fetch_sector_boards(kind="concept", limit=5)
    except Exception:
        pass
    return advance_ratio, rank_map, sector_boards


def _load_portfolio_snapshot(config: AppConfig):
    snapshot_path = config.storage.sqlite_path.resolve().parent.parent / "portfolio-snapshot.json"
    if not snapshot_path.exists():
        return None
    return load_portfolio_snapshot(snapshot_path)


def _load_benchmark_history(config: AppConfig) -> list[StockQuote] | None:
    benchmark = config.monitor.benchmark
    if benchmark is None:
        return None
    provider = _build_provider(config)
    if config.monitor.provider == "eastmoney_minute":
        return provider.fetch_recent_window(benchmark, config.monitor.history_size)
    try:
        return [provider.fetch_quote(benchmark)]
    except Exception:
        return None


def _build_provider(config: AppConfig):
    if config.monitor.provider == "eastmoney_minute":
        return EastmoneyMinuteHistoryProvider(config.monitor)
    return TencentQuoteProvider(config.monitor)


def _load_stock_history(config: AppConfig, conn, provider, stock: StockRef) -> list[StockQuote]:
    if config.monitor.provider == "eastmoney_minute":
        history = provider.fetch_recent_window(stock, config.monitor.history_size)
        if history:
            cache_quotes(conn, history)
        return history
    history = load_recent_quotes(conn, stock.symbol, config.monitor.history_size - 1)
    history.append(provider.fetch_quote(stock))
    return history
