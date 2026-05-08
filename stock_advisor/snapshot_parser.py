"""Parse portfolio text copied from 东方财富 / 同花顺 mobile apps into PortfolioSnapshot."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation


_CODE_RE = re.compile(r"\b(\d{6})\b")
_MONEY_RE = re.compile(r"[\d,]+\.?\d*")

_NAME_HEADERS = {"证券名称", "股票名称", "名称", "品种名称"}
_CODE_HEADERS = {"证券代码", "股票代码", "代码"}
_QTY_HEADERS = {"持仓量", "当前持仓量", "持仓股数", "数量", "股数"}
_COST_HEADERS = {"成本均价", "成本价", "成本", "买入成本"}
_PRICE_HEADERS = {"当前价", "最新价", "现价", "收盘价", "市价"}
_TOTAL_ASSET_KWORDS = {"总资产", "资产总值", "账户总资产", "总市值"}
_CASH_KWORDS = {"可用余额", "现金余额", "资金余额", "可用资金", "可用现金", "资金可用"}


@dataclass
class ParsedHolding:
    name: str
    code: str
    quantity: int
    cost_price: Decimal
    current_price: Decimal


@dataclass
class ParseResult:
    holdings: list[ParsedHolding] = field(default_factory=list)
    total_assets: Decimal | None = None
    cash: Decimal | None = None
    warnings: list[str] = field(default_factory=list)


def parse_portfolio_text(text: str) -> ParseResult:
    result = ParseResult()
    lines = [line.strip() for line in text.splitlines()]

    _parse_asset_info(lines, result)

    col_map: dict[str, int] | None = None
    for i, line in enumerate(lines):
        detected = _detect_header(line)
        if detected:
            col_map = detected
            lines = lines[i + 1:]
            break

    if col_map is not None:
        for line in lines:
            holding = _parse_holding_with_cols(line, col_map)
            if holding:
                result.holdings.append(holding)
    else:
        for line in lines:
            holding = _parse_holding_heuristic(line)
            if holding:
                result.holdings.append(holding)

    seen: set[str] = set()
    deduped: list[ParsedHolding] = []
    for h in result.holdings:
        if h.code not in seen:
            seen.add(h.code)
            deduped.append(h)
    result.holdings = deduped

    if not result.holdings:
        result.warnings.append("未检测到任何持仓行，请确认格式包含6位股票代码")
    return result


def _detect_header(line: str) -> dict[str, int] | None:
    parts = re.split(r"\s+|\t", line)
    col_map: dict[str, int] = {}
    for i, part in enumerate(parts):
        p = part.strip()
        if p in _NAME_HEADERS:
            col_map["name"] = i
        elif p in _CODE_HEADERS:
            col_map["code"] = i
        elif p in _QTY_HEADERS and "qty" not in col_map:
            col_map["qty"] = i
        elif p in _COST_HEADERS:
            col_map["cost"] = i
        elif p in _PRICE_HEADERS:
            col_map["price"] = i
    required = {"code", "qty", "cost"}
    return col_map if required.issubset(col_map) else None


def _parse_holding_with_cols(line: str, col_map: dict[str, int]) -> ParsedHolding | None:
    parts = re.split(r"\s+|\t", line)
    try:
        code = _extract_code(parts, col_map.get("code"))
        if code is None:
            return None
        name = parts[col_map["name"]].strip() if "name" in col_map and col_map["name"] < len(parts) else code
        qty = _safe_int(parts, col_map.get("qty"))
        cost = _safe_decimal(parts, col_map.get("cost"))
        price = _safe_decimal(parts, col_map.get("price")) if "price" in col_map else cost
        if qty is None or cost is None:
            return None
        return ParsedHolding(name=name, code=code, quantity=qty, cost_price=cost, current_price=price or cost)
    except (IndexError, ValueError, InvalidOperation):
        return None


def _parse_holding_heuristic(line: str) -> ParsedHolding | None:
    codes = _CODE_RE.findall(line)
    if not codes:
        return None
    code = codes[0]
    numbers = [Decimal(n.replace(",", "")) for n in _MONEY_RE.findall(line) if _is_price_like(n)]
    if len(numbers) < 2:
        return None
    parts = re.split(r"\s+|\t", line.strip())
    name_candidate = next((p for p in parts if p and not _CODE_RE.match(p) and not re.match(r"[\d.,%+-]+$", p)), code)
    qty = next((int(n) for n in numbers if n == n.to_integral_value() and 100 <= n <= 1_000_000), None)
    if qty is None:
        return None
    prices = [n for n in numbers if Decimal("0.5") < n < Decimal("10000") and n != Decimal(str(qty))]
    if len(prices) < 1:
        return None
    cost = prices[0]
    current = prices[1] if len(prices) >= 2 else cost
    return ParsedHolding(name=name_candidate, code=code, quantity=qty, cost_price=cost, current_price=current)


def _parse_asset_info(lines: list[str], result: ParseResult) -> None:
    for i, line in enumerate(lines):
        parts = re.split(r"\s+|\t", line.strip())
        # single-line: "总资产：46154.01" or inline "总资产 46154.01  可用余额 7942.01"
        for kw in _TOTAL_ASSET_KWORDS:
            if kw in line and result.total_assets is None:
                val = _extract_number_after(line, kw)
                if val is not None:
                    result.total_assets = val
        for kw in _CASH_KWORDS:
            if kw in line and result.cash is None:
                val = _extract_number_after(line, kw)
                if val is not None:
                    result.cash = val
        # two-row: header line has labels, next line has values
        if i + 1 < len(lines) and not any(c.isdigit() for c in line):
            next_parts = re.split(r"\s+|\t", lines[i + 1].strip())
            for j, part in enumerate(parts):
                if j >= len(next_parts):
                    break
                try:
                    val = Decimal(next_parts[j].replace(",", ""))
                except InvalidOperation:
                    continue
                if part in _TOTAL_ASSET_KWORDS and result.total_assets is None:
                    result.total_assets = val
                elif part in _CASH_KWORDS and result.cash is None:
                    result.cash = val


def _extract_code(parts: list[str], idx: int | None) -> str | None:
    if idx is None or idx >= len(parts):
        return None
    val = parts[idx].strip()
    return val if re.fullmatch(r"\d{6}", val) else None


def _safe_int(parts: list[str], idx: int | None) -> int | None:
    if idx is None or idx >= len(parts):
        return None
    try:
        return int(Decimal(parts[idx].replace(",", "")))
    except (InvalidOperation, ValueError):
        return None


def _safe_decimal(parts: list[str], idx: int | None) -> Decimal | None:
    if idx is None or idx >= len(parts):
        return None
    try:
        return Decimal(parts[idx].replace(",", "").rstrip("%"))
    except InvalidOperation:
        return None


def _extract_number_after(line: str, keyword: str) -> Decimal | None:
    idx = line.find(keyword)
    if idx < 0:
        return None
    rest = line[idx + len(keyword):]
    m = _MONEY_RE.search(rest)
    if m:
        try:
            return Decimal(m.group().replace(",", ""))
        except InvalidOperation:
            pass
    return None


def _extract_first_number(line: str) -> Decimal | None:
    m = _MONEY_RE.search(line)
    if m:
        try:
            return Decimal(m.group().replace(",", ""))
        except InvalidOperation:
            pass
    return None


def _is_price_like(s: str) -> bool:
    try:
        val = Decimal(s.replace(",", ""))
        return val > 0
    except InvalidOperation:
        return False
