from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

DOC_MARKDOWN_PATH = Path(__file__).resolve().parent.parent / "data" / "portfolio_doc_latest.md"
SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "portfolio-snapshot.json"

CODE_MAP = {
    "中国卫通": "601698",
    "中兴通讯": "000063",
    "启明星辰": "002439",
    "华天科技": "002185",
    "南网能源": "003035",
    "洛阳钼业": "603993",
}
_ACCOUNT_SUFFIX = re.compile(r"[（(][^)）]*[）)]")


def parse_latest_snapshot(markdown: str) -> dict:
    date_match = re.search(r"#\s*(\d{4}\.\d{1,2}\.\d{1,2})", markdown)
    if not date_match:
        raise RuntimeError("cannot find trade date in doc markdown")
    parts = date_match.group(1).split(".")
    trade_date = f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"

    # Extract only the latest section (from first date heading to next # heading)
    section_start = date_match.start()
    next_heading = re.search(r"\n#\s", markdown[section_start + len(date_match.group(0)):])
    if next_heading:
        section = markdown[section_start:section_start + len(date_match.group(0)) + next_heading.start()]
    else:
        section = markdown[section_start:]

    total_assets = _extract_decimal(section, r"- (?:合并)?总资产[：:]\s*([0-9.]+)")
    cash = _extract_decimal(section, r"- 可用(?:现金|/可取)?[：:]\s*([0-9.]+)")

    holdings: list[dict] = []
    cell_re = re.compile(r"<lark-td>\s*(?P<cell>.*?)\s*</lark-td>", re.S)
    row_re = re.compile(r"<lark-tr>(?P<body>.*?)</lark-tr>", re.S)

    for row_match in row_re.finditer(section):
        cells = [m.group("cell").split(" {align=")[0].strip() for m in cell_re.finditer(row_match.group("body"))]
        if len(cells) < 7:
            continue
        name = cells[0]
        # Strip account suffix like "（东吴）" or "（兴业）"
        name = _ACCOUNT_SUFFIX.sub("", name).strip()
        if name == "股票":
            continue
        code = CODE_MAP.get(name)
        if not code:
            continue
        # Col 4 (0-indexed) is 持仓/可用: "200 / 100" → take first number
        qty_str = cells[4].split("/")[0].strip()
        try:
            quantity = int(qty_str)
        except ValueError:
            continue
        holdings.append(
            {
                "name": name,
                "code": code,
                "quantity": quantity,
                "costPrice": float(cells[5]),
                "currentPrice": float(cells[6]),
            }
        )

    if not holdings:
        raise RuntimeError("cannot parse holdings table from doc markdown")

    return {
        "tradeDate": trade_date,
        "totalAssets": float(total_assets),
        "cash": float(cash),
        "holdings": holdings,
    }


def sync_snapshot_from_doc(
    markdown_path: Path = DOC_MARKDOWN_PATH,
    snapshot_path: Path = SNAPSHOT_PATH,
    *,
    force: bool = False,
    allow_equal_date_overwrite: bool = False,
    allow_rollback: bool = False,
) -> bool:
    if not markdown_path.exists():
        return False

    text = markdown_path.read_text(encoding="utf-8")
    snapshot = parse_latest_snapshot(text)

    if snapshot_path.exists():
        current = _load_snapshot_json(snapshot_path)
        if _should_skip_sync(
            current_snapshot=current,
            incoming_snapshot=snapshot,
            snapshot_path=snapshot_path,
            markdown_path=markdown_path,
            force=force,
            allow_equal_date_overwrite=allow_equal_date_overwrite,
            allow_rollback=allow_rollback,
        ):
            return False

    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def _extract_decimal(text: str, pattern: str) -> Decimal:
    match = re.search(pattern, text)
    if not match:
        raise RuntimeError(f"pattern not found: {pattern}")
    return Decimal(match.group(1))


def _load_snapshot_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _trade_date(snapshot: dict) -> date:
    return date.fromisoformat(str(snapshot["tradeDate"]))


def _should_skip_sync(
    *,
    current_snapshot: dict,
    incoming_snapshot: dict,
    snapshot_path: Path,
    markdown_path: Path,
    force: bool,
    allow_equal_date_overwrite: bool,
    allow_rollback: bool,
) -> bool:
    current_date = _trade_date(current_snapshot)
    incoming_date = _trade_date(incoming_snapshot)

    if incoming_date < current_date and not allow_rollback:
        return True

    if incoming_date == current_date:
        if current_snapshot == incoming_snapshot:
            return True  # identical content — skip (avoid no-op write + notification spam)
        if not allow_equal_date_overwrite:
            return True

    if not force and snapshot_path.stat().st_mtime >= markdown_path.stat().st_mtime:
        return True

    return False
