from __future__ import annotations

import json
from pathlib import Path

OUTBOX_PATH = Path(__file__).resolve().parent.parent / "data" / "direct_dm_outbox.jsonl"
SPOOL_PATH = Path(__file__).resolve().parent.parent / "data" / "direct_dm_spool.txt"


def main() -> int:
    if not OUTBOX_PATH.exists():
        print("NO_OUTBOX")
        return 0

    lines = OUTBOX_PATH.read_text(encoding="utf-8").splitlines()
    pending: list[dict] = []
    sent_any = False

    for line in lines:
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("sent"):
            pending.append(item)
            continue
        sent_any = True
        text = f"{item['title']}\n\n{item['message']}"
        with SPOOL_PATH.open("a", encoding="utf-8") as spool:
            spool.write(text)
            spool.write("\n" + ("=" * 80) + "\n")
        item["sent"] = True
        pending.append(item)

    OUTBOX_PATH.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in pending) + ("\n" if pending else ""), encoding="utf-8")
    print("FLUSHED" if sent_any else "NO_PENDING")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
