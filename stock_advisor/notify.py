from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import requests

from .config import FeishuConfig
from .codex_bridge import queue_codex_notification
from .direct_notify import write_direct_dm
from .feishu_bot_server import FeishuBotClient
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


def send_feishu_app_dm(app_id: str, app_secret: str, receive_open_id: str, title: str, message: str) -> None:
    client = FeishuBotClient(app_id, app_secret)
    text = f"{title}\n\n{message}"
    for chunk in _chunk_text(text):
        client._request(
            "POST",
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "open_id"},
            json_body={
                "receive_id": receive_open_id,
                "msg_type": "text",
                "content": json.dumps({"text": chunk}, ensure_ascii=False),
            },
        )

def _chunk_text(text: str, limit: int = 1800) -> list[str]:
    chunks: list[str] = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks or [""]


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


def deliver_feishu_message(feishu: FeishuConfig, title: str, message: str, *, app_id: str = "", app_secret: str = "") -> None:
    if feishu.delivery_mode == "codex_bridge":
        queue_codex_notification(title, message)
        return
    if feishu.delivery_mode == "direct_dm":
        write_direct_dm(title, message)
        return
    if feishu.delivery_mode == "app_dm":
        if not app_id or not app_secret:
            raise RuntimeError("Feishu app_id/app_secret are required when delivery_mode=app_dm")
        if not feishu.receive_open_id:
            raise RuntimeError("Feishu receive_open_id is required when delivery_mode=app_dm")
        send_feishu_app_dm(app_id, app_secret, feishu.receive_open_id, title, message)
        return
    if not feishu.webhook_url:
        raise RuntimeError("Feishu webhook_url is required when delivery_mode=webhook")
    send_feishu_webhook(feishu.webhook_url, title, message)


def notify_feishu_if_enabled(config, title: str, message: str) -> None:
    if not config.monitor.notification.feishu.enabled:
        return
    deliver_feishu_message(
        config.monitor.notification.feishu,
        title,
        message,
        app_id=config.feishu_bot.app_id,
        app_secret=config.feishu_bot.app_secret,
    )


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
