from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import requests

from .config import FeishuConfig
from .direct_notify import write_direct_dm
from .logging_utils import get_logger


logger = get_logger(__name__)
FAILED_OUTBOX_PATH = Path(__file__).resolve().parent.parent / "data" / "failed_notifications.jsonl"
WEBHOOK_RETRY_DELAYS = (0.5, 1.5, 3.0)


def send_feishu_webhook(webhook_url: str, title: str, message: str) -> None:
    payload = {
        "msg_type": "text",
        "content": {
            "text": f"{title}\n\n{message}"
        },
    }
    last_error: Exception | None = None
    for attempt, delay in enumerate((0.0, *WEBHOOK_RETRY_DELAYS), start=1):
        if delay > 0:
            time.sleep(delay)
        try:
            response = requests.post(webhook_url, json=payload, timeout=8)
            response.raise_for_status()
            if attempt > 1:
                logger.info("Feishu webhook delivered after retry attempt=%s", attempt)
            return
        except requests.RequestException as exc:
            last_error = exc
            logger.warning("Feishu webhook delivery failed attempt=%s error=%s", attempt, exc)
    _queue_failed_notification("webhook", title, message, str(last_error or "unknown error"), target=webhook_url)
    logger.error("Feishu webhook delivery exhausted retries; queued for replay title=%s", title)
    raise RuntimeError(f"Feishu webhook delivery failed after retries: {last_error}")


def deliver_feishu_message(feishu: FeishuConfig, title: str, message: str) -> None:
    if feishu.delivery_mode == "direct_dm":
        write_direct_dm(title, message)
        return
    if not feishu.webhook_url:
        raise RuntimeError("Feishu webhook_url is required when delivery_mode=webhook")
    send_feishu_webhook(feishu.webhook_url, title, message)


def _queue_failed_notification(delivery_mode: str, title: str, message: str, error: str, *, target: str | None = None) -> None:
    FAILED_OUTBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "delivery_mode": delivery_mode,
        "target": target,
        "title": title,
        "message": message,
        "error": error,
        "sent": False,
    }
    with FAILED_OUTBOX_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def flush_failed_notifications() -> tuple[int, int]:
    if not FAILED_OUTBOX_PATH.exists():
        return (0, 0)

    rows = FAILED_OUTBOX_PATH.read_text(encoding="utf-8").splitlines()
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
            logger.warning("Failed notification replay failed title=%s error=%s", item.get("title"), exc)
        pending.append(item)

    suffix = "\n" if pending else ""
    FAILED_OUTBOX_PATH.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in pending) + suffix, encoding="utf-8")
    pending_count = sum(1 for item in pending if not item.get("sent"))
    return (sent_count, pending_count)
