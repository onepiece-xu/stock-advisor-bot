from __future__ import annotations

import json
from pathlib import Path

import requests


OUTBOX_PATH = Path(__file__).resolve().parent.parent / "data" / "failed_notifications.jsonl"


def main() -> int:
    if not OUTBOX_PATH.exists():
        print("NO_OUTBOX")
        return 0

    rows = OUTBOX_PATH.read_text(encoding="utf-8").splitlines()
    pending: list[dict] = []
    sent_count = 0

    for row in rows:
        if not row.strip():
            continue
        item = json.loads(row)
        if item.get("sent"):
            pending.append(item)
            continue

        if item.get("delivery_mode") != "webhook" or not item.get("target"):
            pending.append(item)
            continue

        payload = {
            "msg_type": "text",
            "content": {
                "text": f"{item['title']}\n\n{item['message']}",
            },
        }
        try:
            response = requests.post(item["target"], json=payload, timeout=8)
            response.raise_for_status()
            item["sent"] = True
            sent_count += 1
        except requests.RequestException as exc:
            item["last_error"] = str(exc)
        pending.append(item)

    suffix = "\n" if pending else ""
    OUTBOX_PATH.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in pending) + suffix, encoding="utf-8")
    print(f"FLUSHED {sent_count}" if sent_count else "NO_PENDING")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
