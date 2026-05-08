from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_advisor.portfolio_doc_sync import DOC_MARKDOWN_PATH, SNAPSHOT_PATH, sync_snapshot_from_doc


def main() -> int:
    if not DOC_MARKDOWN_PATH.exists():
        raise SystemExit(f"missing markdown file: {DOC_MARKDOWN_PATH}")
    sync_snapshot_from_doc(force=True)
    print(SNAPSHOT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
