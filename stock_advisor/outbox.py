from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from .platform_compat import lock_file, unlock_file

logger = logging.getLogger(__name__)

OUTBOX_PATH = Path(__file__).resolve().parent.parent / "data" / "outbox.jsonl"
MAX_MESSAGE_LENGTH = 8000  # Feishu text limit ~20KB; keep well under with headroom

# A股最小交易单位：100股（1手）
_MIN_LOT_SIZE = 100
# 匹配消息中的股数：卖出50股、买入150 股、减仓50股等
_QTY_PATTERN = re.compile(r"(\d+)\s*股")

SKIPPED_LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "bridge_skipped.log"


def _validate_a_share_quantity(text: str) -> tuple[bool, str]:
    """Scan message for invalid A-share quantities (< 100 or not multiple of 100).

    Returns (is_valid, reason).  is_valid=False means the message
    contains a quantity that can't be executed on A-share market.
    """
    matches = _QTY_PATTERN.findall(text)
    for m in matches:
        qty = int(m)
        if qty < _MIN_LOT_SIZE:
            return False, f"数量{qty}股低于最小交易单位{_MIN_LOT_SIZE}股（1手），禁止发送"
        if qty % _MIN_LOT_SIZE != 0:
            return False, f"数量{qty}股不是{_MIN_LOT_SIZE}的整数倍，A股必须按手交易"
    return True, ""


def _acquire_outbox_lock() -> int:
    """Acquire an exclusive lock on the outbox file. Returns the fd."""
    OUTBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(OUTBOX_PATH), os.O_RDWR | os.O_CREAT, 0o644)
    lock_file(fd)
    return fd


def _release_outbox_lock(fd: int) -> None:
    """Release the lock and close the fd."""
    try:
        unlock_file(fd)
    finally:
        os.close(fd)


def queue_notification(title: str, message: str) -> None:
    """Queue a notification for delivery via the cron bridge.

    Messages are written to data/outbox.jsonl and picked up by
    the bridge script (stock_advisor_bridge.sh) which delivers
    them to Feishu via webhook.
    """
    OUTBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    # ═══ A股数量校验 ═══
    is_valid, reason = _validate_a_share_quantity(message)
    if not is_valid:
        skip_entry = {
            "skipped_at": datetime.now().isoformat(timespec="seconds"),
            "title": title,
            "reason": reason,
            "message_preview": message[:200],
        }
        try:
            with open(SKIPPED_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(skip_entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
        logger.warning("BLOCKED notification (quantity): %s — %s", title, reason)
        return
    # ═══ /校验 ═══
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


def pull_outbox(limit: int = 20, *, mark_sent: bool = True) -> list[dict[str, str]]:
    """Pull unsent notifications from the outbox.

    Args:
        limit: Max unsent items to pull.
        mark_sent: If True, mark pulled items as sent and write back.

    Returns list of pulled items with keys: created_at, title, message.
    Also trims stale sent entries (>24h) on each call.
    """
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
        cutoff = datetime.now().timestamp() - 24 * 3600  # 24h TTL for sent entries
        trimmed = 0

        for row in rows:
            if not row.strip():
                continue
            item = json.loads(row)

            # ── Trim stale sent entries (>24h) ──
            if item.get("sent"):
                try:
                    ts = datetime.fromisoformat(item["created_at"]).timestamp()
                    if ts < cutoff:
                        trimmed += 1
                        continue
                except (ValueError, KeyError):
                    pass
                pending.append(item)
                continue

            if len(pulled) >= limit:
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

        if (mark_sent and pulled) or trimmed > 0:
            content = "\n".join(json.dumps(item, ensure_ascii=False) for item in pending)
            if pending:
                content += "\n"
            os.lseek(fd, 0, os.SEEK_SET)
            os.truncate(fd, 0)
            os.write(fd, content.encode("utf-8"))
        if trimmed:
            logger.info("Trimmed %d stale sent entries from outbox", trimmed)
    finally:
        _release_outbox_lock(fd)

    return pulled


def flush_outbox() -> bool:
    """Check whether outbox has pending (unsent) notifications.

    This does NOT consume notifications. The cron bridge is the sole
    delivery path. This is a safety check after queuing — confirms
    items were written and are awaiting delivery.

    Returns True if there are unsent notifications, False if empty.
    """
    try:
        pending = pull_outbox(limit=20, mark_sent=False)
        return len(pending) > 0
    except Exception:
        return False


def check_stale(max_age_minutes: int = 5) -> list[dict]:
    """Check for unsent notifications older than max_age_minutes.

    Returns list of stale notifications that haven't been delivered.
    Used as a health check — if notifications are piling up, the
    bridge may have stalled.
    """
    cutoff = datetime.now().timestamp() - max_age_minutes * 60
    pending = pull_outbox(limit=50, mark_sent=False)
    stale = []
    for item in pending:
        try:
            ts = datetime.fromisoformat(item["created_at"]).timestamp()
            if ts < cutoff:
                stale.append(item)
        except (ValueError, KeyError):
            pass
    return stale
