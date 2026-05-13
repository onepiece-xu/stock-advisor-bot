from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


OUTBOX_PATH = Path(__file__).resolve().parent.parent / "data" / "codex_outbox.jsonl"
MAX_MESSAGE_LENGTH = 8000  # Feishu text limit ~20KB; keep well under with headroom


def queue_codex_notification(title: str, message: str) -> None:
    OUTBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    if len(message) > MAX_MESSAGE_LENGTH:
        message = message[:MAX_MESSAGE_LENGTH]
        last_nl = message.rfind("\n", MAX_MESSAGE_LENGTH - 500)
        if last_nl > MAX_MESSAGE_LENGTH // 2:
            message = message[:last_nl]
        message += "\n\n[消息过长已截断，完整内容见 daemon 日志]"
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "title": title,
        "message": message,
        "sent": False,
    }
    with OUTBOX_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def pull_codex_notifications(limit: int = 20, *, mark_sent: bool = True) -> list[dict[str, str]]:
    if limit <= 0 or not OUTBOX_PATH.exists():
        return []

    rows = OUTBOX_PATH.read_text(encoding="utf-8").splitlines()
    pending: list[dict] = []
    pulled: list[dict[str, str]] = []

    for row in rows:
        if not row.strip():
            continue
        item = json.loads(row)
        if item.get("sent") or len(pulled) >= limit:
            pending.append(item)
            continue
        pulled.append(
            {
                "created_at": str(item.get("created_at", "")),
                "title": str(item.get("title", "")),
                "message": str(item.get("message", "")),
            }
        )
        if mark_sent:
            item["sent"] = True
        pending.append(item)

    if mark_sent:
        suffix = "\n" if pending else ""
        OUTBOX_PATH.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in pending) + suffix,
            encoding="utf-8",
        )

    return pulled
