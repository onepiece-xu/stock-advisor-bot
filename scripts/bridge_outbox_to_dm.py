from __future__ import annotations

import json
from pathlib import Path

OUTBOX_PATH = Path(__file__).resolve().parent.parent / "data" / "direct_dm_outbox.jsonl"
BRIDGE_PAYLOAD_PATH = Path(__file__).resolve().parent.parent / "data" / "bridge_payload.txt"


def main() -> int:
    if not OUTBOX_PATH.exists():
        print("NO_OUTBOX")
        return 0

    lines = OUTBOX_PATH.read_text(encoding="utf-8").splitlines()
    pending: list[dict] = []
    messages: list[str] = []

    for line in lines:
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("sent"):
            pending.append(item)
            continue
        messages.append(f"{item['title']}\n\n{item['message']}")
        item["sent"] = True
        pending.append(item)

    OUTBOX_PATH.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in pending) + ("\n" if pending else ""),
        encoding="utf-8",
    )

    if not messages:
        print("NO_PENDING")
        return 0

    BRIDGE_PAYLOAD_PATH.write_text("\n\n" + ("-" * 40) + "\n\n".join(messages), encoding="utf-8")
    print(BRIDGE_PAYLOAD_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
