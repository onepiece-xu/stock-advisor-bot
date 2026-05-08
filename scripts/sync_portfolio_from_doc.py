from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stock_advisor.portfolio_doc_sync import DOC_MARKDOWN_PATH, SNAPSHOT_PATH, sync_snapshot_from_doc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync portfolio-snapshot.json from cached Feishu markdown")
    parser.add_argument("--force", action="store_true", help="Ignore file mtime guard")
    parser.add_argument(
        "--allow-equal-date-overwrite",
        action="store_true",
        help="Allow doc snapshot to overwrite a different local snapshot from the same trade date",
    )
    parser.add_argument(
        "--allow-rollback",
        action="store_true",
        help="Allow an older doc date to overwrite a newer local snapshot",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not DOC_MARKDOWN_PATH.exists():
        raise SystemExit(f"missing markdown file: {DOC_MARKDOWN_PATH}")
    synced = sync_snapshot_from_doc(
        force=args.force,
        allow_equal_date_overwrite=args.allow_equal_date_overwrite,
        allow_rollback=args.allow_rollback,
    )
    print(SNAPSHOT_PATH if synced else f"skipped: {SNAPSHOT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
