from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from .logging_utils import get_logger
from .models import StockQuote


_NEWS_CACHE_TTL = timedelta(minutes=10)
_news_cache: dict[str, tuple[list, datetime]] = {}
logger = get_logger(__name__)

_THS_NEWS_URL = "https://news.10jqka.com.cn/tapp/news/push/stock/"
_EM_ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://stockpage.10jqka.com.cn/"}


@dataclass(slots=True)
class NewsItem:
    title: str
    link: str
    source: str
    published_at: str


def fetch_stock_news(quote: StockQuote, *, limit: int = 3) -> list[NewsItem]:
    cache_key = f"{quote.code}:{limit}"
    cached = _news_cache.get(cache_key)
    if cached is not None:
        items, cached_at = cached
        if datetime.now() - cached_at < _NEWS_CACHE_TTL:
            return items

    seen: set[str] = set()
    items: list[NewsItem] = []

    for item in _fetch_ths_news(quote.code, limit=limit):
        if item.title not in seen:
            seen.add(item.title)
            items.append(item)
        if len(items) >= limit:
            break

    if len(items) < limit:
        for item in _fetch_em_announcements(quote.code, limit=limit - len(items)):
            if item.title not in seen:
                seen.add(item.title)
                items.append(item)
            if len(items) >= limit:
                break

    _news_cache[cache_key] = (items, datetime.now())
    return items


def fetch_announcements_for_code(code: str, *, limit: int = 3) -> list[NewsItem]:
    return _fetch_em_announcements(code, limit=limit)


def render_news_lines(items: list[NewsItem]) -> list[str]:
    if not items:
        return ["新闻：暂无近期相关资讯，请关注公告和板块异动。"]
    lines = ["新闻："]
    for item in items:
        lines.append(f"- {item.title} | {item.source} | {item.published_at}")
    return lines


def _fetch_ths_news(code: str, *, limit: int) -> list[NewsItem]:
    try:
        r = requests.get(
            _THS_NEWS_URL,
            params={"page": 1, "tag": "", "limit": limit, "ver": "1", "stockcode": code, "qs": 1},
            headers=_HEADERS,
            timeout=5,
        )
        r.raise_for_status()
        data = r.json().get("data") or {}
        items = []
        for row in (data.get("list") or [])[:limit]:
            title = (row.get("title") or "").strip()
            if not title:
                continue
            pub = _fmt_ctime(row.get("ctime", ""))
            items.append(NewsItem(title=title, link=row.get("url", ""), source="同花顺", published_at=pub))
        return items
    except Exception as exc:  # noqa: BLE001
        logger.warning("THS news fetch failed code=%s error=%s", code, exc)
        return []


def _fetch_em_announcements(code: str, *, limit: int) -> list[NewsItem]:
    try:
        r = requests.get(
            _EM_ANN_URL,
            params={
                "sr": "-1",
                "page_size": str(limit),
                "page_index": "1",
                "ann_type": "A",
                "client_source": "web",
                "stock_list": code,
                "f_node": "0",
                "second_contract_id": "",
            },
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.eastmoney.com/"},
            timeout=5,
        )
        r.raise_for_status()
        rows = (r.json().get("data") or {}).get("list") or []
        items = []
        for row in rows[:limit]:
            title = (row.get("title") or "").strip()
            if not title:
                continue
            pub = _fmt_notice_date(row.get("notice_date", ""))
            items.append(NewsItem(title=title, link="", source="东方财富公告", published_at=pub))
        return items
    except Exception as exc:  # noqa: BLE001
        logger.warning("EM announcement fetch failed code=%s error=%s", code, exc)
        return []


def _fmt_ctime(ctime: str) -> str:
    if not ctime:
        return "时间未知"
    try:
        ts = int(ctime)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return str(ctime)


def _fmt_notice_date(text: str) -> str:
    if not text:
        return "时间未知"
    try:
        return datetime.fromisoformat(text[:16]).strftime("%m-%d %H:%M")
    except Exception:
        return text[:10]
