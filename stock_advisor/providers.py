from __future__ import annotations

from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

import requests

from .config import MonitorConfig
from .models import StockQuote, StockRef


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
