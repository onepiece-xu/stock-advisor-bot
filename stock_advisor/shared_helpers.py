"""Shared helper functions used by both cli.py and feishu_bot_server.py."""

from datetime import datetime
from decimal import Decimal

from .logging_utils import get_logger
from .models import StockQuote, StockRef
from .providers import EastmoneyMarketSnapshotProvider, EastmoneyMinuteHistoryProvider, TencentQuoteProvider
from .storage import cache_quotes, load_recent_quotes

logger = get_logger(__name__)


def parse_history_datetime(text: str) -> datetime:
    normalized = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    raise RuntimeError(f"无法解析历史时点: {text}")


def build_provider(config):
    if config.monitor.provider == "eastmoney_minute":
        return EastmoneyMinuteHistoryProvider(config.monitor)
    return TencentQuoteProvider(config.monitor)


def load_stock_history(config, conn, provider, stock) -> list[StockQuote]:
    if config.monitor.provider == "eastmoney_minute":
        history = provider.fetch_recent_window(stock, config.monitor.history_size)
        if history:
            cache_quotes(conn, history)
        return history
    history = load_recent_quotes(conn, stock.symbol, config.monitor.history_size - 1)
    history.append(provider.fetch_quote(stock))
    return history


def load_market_context(config) -> tuple[Decimal, dict[str, int], list[dict]]:
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
    except Exception as exc:
        logger.warning("Market context load failed error=%s", exc)
    return advance_ratio, rank_map, sector_boards
