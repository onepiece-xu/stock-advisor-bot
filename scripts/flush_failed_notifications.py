from __future__ import annotations

from stock_advisor.notify import FAILED_OUTBOX_PATH, flush_failed_notifications


def main() -> int:
    if not OUTBOX_PATH.exists():
        print("NO_OUTBOX")
        return 0
    sent_count, pending_count = flush_failed_notifications()
    if sent_count:
        print(f"FLUSHED {sent_count}")
    elif pending_count:
        print(f"PENDING {pending_count}")
    else:
        print("NO_PENDING")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
