from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from .logging_utils import get_logger
from .models import StockQuote


_NEWS_CACHE_TTL = timedelta(minutes=10)
_news_cache: dict[str, tuple[list, datetime]] = {}
logger = get_logger(__name__)

_THS_NEWS_URL = "https://news.10jqka.com.cn/tapp/news/push/stock/"
_EM_ANN_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://stockpage.10jqka.com.cn/"}

# Important announcement keywords — used to highlight critical announcements
IMPORTANT_ANNOUNCEMENT_KEYWORDS = [
    "减持", "增持", "回购",
    "年报", "半年报", "季报", "业绩预告", "业绩快报",
    "ST", "*ST", "退市", "风险警示", "暂停上市",
    "解禁", "限售股", "上市流通",
    "重大资产重组", "重组", "收购", "合并",
    "分红", "送转", "除权", "除息",
    "诉讼", "仲裁", "违规", "立案", "处罚",
    "停牌", "复牌",
    "实际控制人", "控股股东", "变更",
    "非公开发行", "定增", "配股",
]


def is_important_announcement(title: str) -> bool:
    """Check if an announcement title contains important keywords."""
    for kw in IMPORTANT_ANNOUNCEMENT_KEYWORDS:
        if kw in title:
            return True
    return False


# Daily announcement dedup cache file
_ANN_CACHE_DIR = Path("data/portfolio")
_ANN_CACHE_FILE = _ANN_CACHE_DIR / "announcement-seen.json"


def _load_seen_announcements() -> set[str]:
    """Load previously seen announcement titles for deduplication."""
    if not _ANN_CACHE_FILE.exists():
        return set()
    try:
        data = json.loads(_ANN_CACHE_FILE.read_text(encoding="utf-8"))
        return set(data.get("seen_titles", []))
    except Exception:
        return set()


def _save_seen_announcements(seen: set[str]) -> None:
    """Save seen announcement titles for deduplication."""
    _ANN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "seen_titles": list(seen),
    }
    _ANN_CACHE_FILE.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def filter_new_announcements(items: list[NewsItem]) -> list[NewsItem]:
    """Filter out previously seen announcements, return only new ones.
    Updates the seen cache with new items."""
    seen = _load_seen_announcements()
    new_items = []
    for item in items:
        if item.title not in seen:
            new_items.append(item)
            seen.add(item.title)
    if new_items:
        _save_seen_announcements(seen)
    return new_items


def format_announcement_line(item: NewsItem) -> str:
    """Format a single announcement line, marking important ones with ⚠️."""
    prefix = "⚠️ " if is_important_announcement(item.title) else ""
    return f"- {prefix}{item.title} | {item.source} | {item.published_at}"


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
    except Exception as exc:
        logger.warning("Failed to format ctime error=%s", exc)
        return str(ctime)


def _fmt_notice_date(text: str) -> str:
    if not text:
        return "时间未知"
    try:
        return datetime.fromisoformat(text[:16]).strftime("%m-%d %H:%M")
    except Exception as exc:
        logger.warning("Failed to format notice date error=%s", exc)
        return text[:10]
