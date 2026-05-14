from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime
from pathlib import Path


OUTBOX_PATH = Path(__file__).resolve().parent.parent / "data" / "codex_outbox.jsonl"
MAX_MESSAGE_LENGTH = 8000  # Feishu text limit ~20KB; keep well under with headroom


def _acquire_outbox_lock() -> int:
    """Acquire an exclusive lock on the outbox file. Returns the fd."""
    OUTBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(OUTBOX_PATH), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _release_outbox_lock(fd: int) -> None:
    """Release the lock and close the fd."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


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
    fd = _acquire_outbox_lock()
    try:
        os.lseek(fd, 0, os.SEEK_END)
        os.write(fd, (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        _release_outbox_lock(fd)


def pull_codex_notifications(limit: int = 20, *, mark_sent: bool = True) -> list[dict[str, str]]:
    if limit <= 0 or not OUTBOX_PATH.exists():
        return []

    fd = _acquire_outbox_lock()
    try:
        # Read all content
        os.lseek(fd, 0, os.SEEK_SET)
        data = os.read(fd, 10 * 1024 * 1024)  # 10MB max
        rows = data.decode("utf-8").splitlines()

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

        if mark_sent and pulled:
            # Write back with updated sent flags
            content = "\n".join(json.dumps(item, ensure_ascii=False) for item in pending)
            if pending:
                content += "\n"
            os.lseek(fd, 0, os.SEEK_SET)
            os.truncate(fd, 0)
            os.write(fd, content.encode("utf-8"))
    finally:
        _release_outbox_lock(fd)

    return pulled


def flush_codex_bridge() -> bool:
    """Verify pending notifications are queued for the cron bridge to deliver.

    IMPORTANT: This does NOT consume notifications.  The cron bridge job
    (every 1 min) is the sole delivery path.  This function only checks
    that outbox has pending items — a safety check after queuing.

    Returns True if there are pending (unsent) notifications, False if empty.
    """
    try:
        pending = pull_codex_notifications(limit=20, mark_sent=False)
        return len(pending) > 0
    except Exception:
        return False


def check_stale_notifications(max_age_minutes: int = 5) -> list[dict]:
    """Check for unsent notifications older than max_age_minutes.

    Returns list of stale notifications that haven't been delivered.
    Used as a health check — if notifications are piling up, the cron
    bridge may have stalled.
    """
    cutoff = datetime.now().timestamp() - max_age_minutes * 60
    pending = pull_codex_notifications(limit=50, mark_sent=False)
    stale = []
    for item in pending:
        try:
            ts = datetime.fromisoformat(item["created_at"]).timestamp()
            if ts < cutoff:
                stale.append(item)
        except (ValueError, KeyError):
            pass
    return stale
