from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import requests

from .logging_utils import get_logger
from .models import StockQuote


GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
logger = get_logger(__name__)


@dataclass(slots=True)
class NewsItem:
    title: str
    link: str
    source: str
    published_at: str


def fetch_stock_news(quote: StockQuote, *, limit: int = 3) -> list[NewsItem]:
    queries = [f'{quote.code} 股票', f'{quote.name} 股票', quote.code, quote.name]
    seen: set[str] = set()
    items: list[NewsItem] = []
    for query in queries:
        for item in _fetch_google_news(query, limit=limit):
            key = item.title.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(item)
            if len(items) >= limit:
                return items
    return items


def render_news_lines(items: list[NewsItem]) -> list[str]:
    if not items:
        return ["新闻：暂无抓到高相关实时新闻，可继续观察公告和板块异动。"]
    lines = ["新闻："]
    for item in items:
        lines.append(f"- {item.title} | {item.source} | {item.published_at}")
    return lines


def _fetch_google_news(query: str, *, limit: int) -> list[NewsItem]:
    params = {
        "q": f"{query} when:3d",
        "hl": "zh-CN",
        "gl": "CN",
        "ceid": "CN:zh-Hans",
    }
    try:
        response = requests.get(GOOGLE_NEWS_RSS, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        response.raise_for_status()
        return _parse_google_news_rss(response.text, limit=limit)
    except requests.RequestException as exc:
        logger.warning("Google News RSS fetch failed query=%s error=%s", query, exc)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.exception("Google News RSS parse failed query=%s error=%s", query, exc)
        return []


def _parse_google_news_rss(xml_text: str, *, limit: int) -> list[NewsItem]:
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return []
    items: list[NewsItem] = []
    for item in channel.findall("item")[:limit]:
        title = _clean_text(item.findtext("title", default=""))
        link = item.findtext("link", default="").strip()
        source = _clean_text(item.findtext("source", default="Google News"))
        pub_date = _format_pub_date(item.findtext("pubDate", default=""))
        if title:
            items.append(NewsItem(title=title, link=link, source=source, published_at=pub_date))
    return items


def _format_pub_date(text: str) -> str:
    text = text.strip()
    if not text:
        return "时间未知"
    try:
        dt = parsedate_to_datetime(text)
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return text


def _clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text
