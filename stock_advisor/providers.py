from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from time import sleep

import requests

from .config import MonitorConfig
from .logging_utils import get_logger
from .models import StockQuote, StockRef


logger = get_logger(__name__)


class TencentQuoteProvider:
    def __init__(self, monitor_config: MonitorConfig) -> None:
        self.monitor_config = monitor_config

    def fetch_quote(self, stock: StockRef) -> StockQuote:
        url = f"{self.monitor_config.provider_settings.tencent_base_url}{stock.symbol}"
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=self.monitor_config.provider_settings.request_timeout_ms / 1000,
        )
        response.encoding = "gbk"
        raw_body = response.text
        payload = self._extract_payload(raw_body)
        fields = payload.split("~")
        if len(fields) < 38:
            raise RuntimeError(f"Unexpected Tencent response: {raw_body}")

        current_price = self._decimal(fields[3])
        previous_close = self._decimal(fields[4])
        open_price = self._decimal(fields[5])
        high_price = self._decimal(fields[33])
        low_price = self._decimal(fields[34])
        change_amount = self._safe_decimal(fields[31], current_price - previous_close)
        change_percent = self._safe_decimal(fields[32], self._calculate_percent(current_price, previous_close))
        volume_hands = self._safe_decimal(fields[36], self._safe_decimal(fields[6], Decimal("0")))
        turnover_wan = self._safe_decimal(fields[37], Decimal("0"))
        quote_time = self._parse_time(fields[30] if len(fields) > 30 else "")

        return StockQuote(
            provider="tencent",
            symbol=stock.symbol,
            code=stock.code,
            name=fields[1].strip(),
            current_price=current_price,
            open_price=open_price,
            previous_close=previous_close,
            high_price=high_price,
            low_price=low_price,
            change_amount=change_amount,
            change_percent=change_percent,
            volume_shares=(volume_hands * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP),
            turnover_yuan=(turnover_wan * Decimal("10000")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            quote_time=quote_time,
            raw_payload=raw_body,
        )

    def _extract_payload(self, raw_body: str) -> str:
        first_quote = raw_body.find('"')
        last_quote = raw_body.rfind('"')
        if first_quote < 0 or last_quote <= first_quote:
            raise RuntimeError(f"Cannot parse Tencent response: {raw_body}")
        return raw_body[first_quote + 1:last_quote]

    def _decimal(self, value: str) -> Decimal:
        return Decimal(value.strip())

    def _safe_decimal(self, value: str, fallback: Decimal) -> Decimal:
        value = (value or "").strip()
        return fallback if not value else Decimal(value)

    def _parse_time(self, text: str) -> datetime:
        text = (text or "").strip()
        if not text:
            return datetime.now()
        return datetime.strptime(text, "%Y%m%d%H%M%S")

    def _calculate_percent(self, current_price: Decimal, previous_close: Decimal) -> Decimal:
        if previous_close <= 0:
            return Decimal("0")
        return ((current_price - previous_close) / previous_close * Decimal("100")).quantize(Decimal("0.01"))


class EastmoneyMinuteHistoryProvider:
    def __init__(self, monitor_config: MonitorConfig) -> None:
        self.monitor_config = monitor_config

    def fetch_quotes(self, stock: StockRef, start_date: date, end_date: date) -> list[StockQuote]:
        response = requests.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": self._secid(stock),
                "klt": "1",
                "fqt": "1",
                "lmt": "10000",
                "beg": start_date.strftime("%Y%m%d"),
                "end": end_date.strftime("%Y%m%d"),
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=self.monitor_config.provider_settings.request_timeout_ms / 1000,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        klines = data.get("klines") or []
        if not klines:
            return []

        quotes: list[StockQuote] = []
        current_trade_date: date | None = None
        day_open = Decimal("0")
        day_high = Decimal("0")
        day_low = Decimal("0")
        day_volume = Decimal("0")
        day_turnover = Decimal("0")
        name = str(data.get("name") or stock.code)

        for line in klines:
            fields = str(line).split(",")
            if len(fields) < 10:
                continue

            quote_time = datetime.strptime(fields[0], "%Y-%m-%d %H:%M")
            trade_date = quote_time.date()
            bar_open = Decimal(fields[1])
            current_price = Decimal(fields[2])
            bar_high = Decimal(fields[3])
            bar_low = Decimal(fields[4])
            minute_volume_shares = Decimal(fields[5]) * Decimal("100")
            minute_turnover = Decimal(fields[6])
            change_percent = Decimal(fields[8])
            change_amount = Decimal(fields[9])
            previous_close = (current_price - change_amount).quantize(Decimal("0.01"))

            if trade_date != current_trade_date:
                current_trade_date = trade_date
                day_open = bar_open
                day_high = bar_high
                day_low = bar_low
                day_volume = Decimal("0")
                day_turnover = Decimal("0")
            else:
                day_high = max(day_high, bar_high)
                day_low = min(day_low, bar_low)

            day_volume += minute_volume_shares
            day_turnover += minute_turnover

            quotes.append(
                StockQuote(
                    provider="eastmoney_minute",
                    symbol=stock.symbol,
                    code=stock.code,
                    name=name,
                    current_price=current_price,
                    open_price=day_open,
                    previous_close=previous_close,
                    high_price=day_high,
                    low_price=day_low,
                    change_amount=change_amount,
                    change_percent=change_percent,
                    volume_shares=day_volume.quantize(Decimal("1"), rounding=ROUND_HALF_UP),
                    turnover_yuan=day_turnover.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                    quote_time=quote_time,
                    raw_payload=str(line),
                )
            )

        return quotes

    def fetch_recent_window(
        self,
        stock: StockRef,
        count: int,
        *,
        end_time: datetime | None = None,
    ) -> list[StockQuote]:
        if count <= 0:
            return []
        if end_time is None:
            end_date = datetime.now().date()
        else:
            end_date = end_time.date()
        quotes = self._fetch_recent_multiday_quotes(stock, end_date)
        if end_time is not None:
            quotes = [quote for quote in quotes if quote.quote_time <= end_time]
        return quotes[-count:]

    def fetch_recent_window_exact(
        self,
        stock: StockRef,
        count: int,
        *,
        end_time: datetime | None = None,
    ) -> list[StockQuote]:
        if count <= 0:
            return []
        effective_end = end_time or datetime.now()
        start_date = effective_end.date() - timedelta(days=_calendar_lookback_days(count))
        quotes = self.fetch_quotes(stock, start_date, effective_end.date())
        if end_time is not None:
            quotes = [quote for quote in quotes if quote.quote_time <= end_time]
        return quotes[-count:]

    def fetch_recent_days_exact(
        self,
        stock: StockRef,
        ndays: int,
        *,
        end_date: date | None = None,
    ) -> list[StockQuote]:
        if ndays <= 0:
            return []
        effective_end = end_date or datetime.now().date()
        lookback_days = max(7, ndays * 4)
        quotes = self.fetch_quotes(stock, effective_end - timedelta(days=lookback_days), effective_end)
        return _tail_trade_days(quotes, ndays)

    def _secid(self, stock: StockRef) -> str:
        exchange = stock.exchange.lower()
        if exchange == "sh":
            market = "1"
        elif exchange == "sz":
            market = "0"
        else:
            raise RuntimeError(f"Unsupported exchange for eastmoney minute history: {stock.exchange}")
        return f"{market}.{stock.code}"

    def _fetch_recent_multiday_quotes(self, stock: StockRef, end_date: date) -> list[StockQuote]:
        trend_quotes = self._fetch_trend_quotes(stock, ndays=5)
        day_quotes = self.fetch_quotes(stock, end_date, end_date)
        if day_quotes:
            trend_quotes = [quote for quote in trend_quotes if quote.quote_time.date() < end_date]
            return trend_quotes + day_quotes
        return trend_quotes

    def _fetch_trend_quotes(self, stock: StockRef, *, ndays: int) -> list[StockQuote]:
        response = requests.get(
            "https://push2his.eastmoney.com/api/qt/stock/trends2/get",
            params={
                "secid": self._secid(stock),
                "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "ndays": str(max(1, min(ndays, 5))),
                "iscr": "0",
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=self.monitor_config.provider_settings.request_timeout_ms / 1000,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or {}
        trends = data.get("trends") or []
        if not trends:
            return []

        quotes: list[StockQuote] = []
        current_trade_date: date | None = None
        day_open = Decimal("0")
        day_high = Decimal("0")
        day_low = Decimal("0")
        day_volume = Decimal("0")
        day_turnover = Decimal("0")
        name = str(data.get("name") or stock.code)

        for line in trends:
            fields = str(line).split(",")
            if len(fields) < 7:
                continue
            quote_time = datetime.strptime(fields[0], "%Y-%m-%d %H:%M")
            trade_date = quote_time.date()
            current_price = Decimal(fields[2])
            high_price = Decimal(fields[3])
            low_price = Decimal(fields[4])
            minute_volume_shares = Decimal(fields[5]) * Decimal("100")
            minute_turnover = Decimal(fields[6])

            if trade_date != current_trade_date:
                current_trade_date = trade_date
                day_open = current_price
                day_high = high_price
                day_low = low_price
                day_volume = Decimal("0")
                day_turnover = Decimal("0")
            else:
                day_high = max(day_high, high_price)
                day_low = min(day_low, low_price)

            day_volume += minute_volume_shares
            day_turnover += minute_turnover

            quotes.append(
                StockQuote(
                    provider="eastmoney_minute",
                    symbol=stock.symbol,
                    code=stock.code,
                    name=name,
                    current_price=current_price,
                    open_price=day_open,
                    previous_close=current_price,
                    high_price=day_high,
                    low_price=day_low,
                    change_amount=Decimal("0"),
                    change_percent=Decimal("0"),
                    volume_shares=day_volume.quantize(Decimal("1"), rounding=ROUND_HALF_UP),
                    turnover_yuan=day_turnover.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                    quote_time=quote_time,
                    raw_payload=str(line),
                )
            )

        return quotes


class EastmoneyMarketSnapshotProvider:
    def __init__(self, monitor_config: MonitorConfig) -> None:
        self.monitor_config = monitor_config
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://quote.eastmoney.com/",
            }
        )

    def fetch_market_breadth(self) -> dict:
        rows = self._fetch_clist_all(
            fs="m:0+t:6,m:0+t:13,m:1+t:2,m:1+t:23",
            fields="f12,f14,f3",
            page_size=200,
            sort_field="f12",
            descending=False,
        )
        up_count = sum(1 for item in rows if item.get("f3") is not None and float(item["f3"]) > 0)
        flat_count = sum(1 for item in rows if item.get("f3") is not None and float(item["f3"]) == 0)
        down_count = sum(1 for item in rows if item.get("f3") is not None and float(item["f3"]) < 0)
        return {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "up_count": up_count,
            "flat_count": flat_count,
            "down_count": down_count,
        }

    def fetch_top_stocks(self, *, limit: int = 5, descending: bool = True) -> list[dict]:
        rows = self._fetch_clist(
            fs="m:0+t:6,m:0+t:13,m:1+t:2,m:1+t:23",
            fields="f12,f14,f2,f3,f6,f100,f102",
            page_size=limit,
            sort_field="f3",
            descending=descending,
        )
        return [
            {
                "code": str(item.get("f12", "")),
                "name": str(item.get("f14", "")),
                "current_price": float(item.get("f2", 0) or 0),
                "change_percent": float(item.get("f3", 0) or 0),
                "turnover_yi": float(item.get("f6", 0) or 0) / 100000000,
                "industry_name": str(item.get("f100", "") or ""),
                "concept_name": str(item.get("f102", "") or ""),
            }
            for item in rows
        ]

    def fetch_sector_boards(self, *, kind: str, limit: int = 5) -> list[dict]:
        fs = {
            "industry": "m:90+t:2",
            "concept": "m:90+t:3",
            "region": "m:90+t:1",
        }.get(kind)
        if fs is None:
            raise RuntimeError(f"Unsupported sector board kind: {kind}")
        rows = self._fetch_clist(
            fs=fs,
            fields="f12,f14,f2,f3,f6,f104,f105,f128,f136,f140",
            page_size=limit,
            sort_field="f3",
            descending=True,
        )
        return [
            {
                "code": str(item.get("f12", "")),
                "name": str(item.get("f14", "")),
                "change_percent": float(item.get("f3", 0) or 0),
                "turnover_yi": float(item.get("f6", 0) or 0) / 100000000,
                "up_count": int(item.get("f104", 0) or 0),
                "down_count": int(item.get("f105", 0) or 0),
                "leader_name": str(item.get("f128", "") or ""),
                "leader_code": str(item.get("f140", "") or ""),
                "leader_change_percent": float(item.get("f136", 0) or 0),
            }
            for item in rows
        ]

    def _fetch_clist(
        self,
        *,
        fs: str,
        fields: str,
        page_size: int,
        sort_field: str,
        descending: bool,
        page: int = 1,
    ) -> list[dict]:
        rows, _ = self._fetch_clist_page(
            fs=fs,
            fields=fields,
            page_size=page_size,
            sort_field=sort_field,
            descending=descending,
            page=page,
        )
        return rows

    def _fetch_clist_page(
        self,
        *,
        fs: str,
        fields: str,
        page_size: int,
        sort_field: str,
        descending: bool,
        page: int,
    ) -> tuple[list[dict], int]:
        payload = self._request_json(
            "https://push2.eastmoney.com/api/qt/clist/get",
            params={
                "pn": str(max(1, page)),
                "pz": str(max(1, page_size)),
                "po": "1" if descending else "0",
                "np": "1",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": "2",
                "invt": "2",
                "fid": sort_field,
                "fs": fs,
                "fields": fields,
            },
        )
        data = payload.get("data") or {}
        return list(data.get("diff") or []), int(data.get("total") or 0)

    def _fetch_clist_all(
        self,
        *,
        fs: str,
        fields: str,
        page_size: int,
        sort_field: str,
        descending: bool,
    ) -> list[dict]:
        page = 1
        rows: list[dict] = []
        seen_codes: set[str] = set()
        total = 0
        while True:
            chunk, total = self._fetch_clist_page(
                fs=fs,
                fields=fields,
                page_size=page_size,
                sort_field=sort_field,
                descending=descending,
                page=page,
            )
            if not chunk:
                break
            added = 0
            for item in chunk:
                code = str(item.get("f12") or "").strip()
                if code and code in seen_codes:
                    continue
                if code:
                    seen_codes.add(code)
                rows.append(item)
                added += 1
            if total > 0 and len(seen_codes) >= total:
                break
            if len(chunk) < page_size:
                break
            if added == 0:
                logger.warning("Eastmoney market snapshot pagination stopped because a page contained only duplicates")
                break
            page += 1
            if total > 0 and len(rows) >= total:
                break
            if page > 100:
                logger.warning("Eastmoney market snapshot pagination hit the safety page limit")
                break
        return rows

    def _request_json(self, url: str, *, params: dict[str, str]) -> dict:
        last_error: Exception | None = None
        timeout_seconds = max(self.monitor_config.provider_settings.request_timeout_ms / 1000, 3)
        for attempt in range(3):
            try:
                response = self._session.get(url, params=params, timeout=timeout_seconds)
                response.raise_for_status()
                return dict(response.json() or {})
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt < 2:
                    logger.warning(
                        "Eastmoney request failed, retrying (%s/3): %s",
                        attempt + 1,
                        exc,
                    )
                    sleep(0.6 * (attempt + 1))
                    continue
                raise RuntimeError(f"Eastmoney request failed after retries: {exc}") from exc
        raise RuntimeError(f"Eastmoney request failed: {last_error}")


def _calendar_lookback_days(count: int) -> int:
    trading_days = max((count + 239) // 240, 1)
    return max(7, trading_days * 4)


def _tail_trade_days(quotes: list[StockQuote], ndays: int) -> list[StockQuote]:
    if ndays <= 0 or not quotes:
        return []
    seen_dates: list[date] = []
    selected_dates: set[date] = set()
    for quote in reversed(quotes):
        trade_date = quote.quote_time.date()
        if trade_date in selected_dates:
            continue
        selected_dates.add(trade_date)
        seen_dates.append(trade_date)
        if len(seen_dates) >= ndays:
            break
    keep_dates = set(seen_dates)
    return [quote for quote in quotes if quote.quote_time.date() in keep_dates]
