from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


OUTBOX_PATH = Path(__file__).resolve().parent.parent / "data" / "direct_dm_outbox.jsonl"


def write_direct_dm(title: str, message: str) -> None:
    OUTBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "title": title,
        "message": message,
        "sent": False,
    }
    with OUTBOX_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
