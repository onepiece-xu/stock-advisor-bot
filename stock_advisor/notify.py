from __future__ import annotations

import requests

from .config import FeishuConfig
from .direct_notify import write_direct_dm


def send_feishu_webhook(webhook_url: str, title: str, message: str) -> None:
    payload = {
        "msg_type": "text",
        "content": {
            "text": f"{title}\n\n{message}"
        },
    }
    response = requests.post(webhook_url, json=payload, timeout=8)
    response.raise_for_status()


def deliver_feishu_message(feishu: FeishuConfig, title: str, message: str) -> None:
    if feishu.delivery_mode == "direct_dm":
        write_direct_dm(title, message)
        return
    if not feishu.webhook_url:
        raise RuntimeError("Feishu webhook_url is required when delivery_mode=webhook")
    send_feishu_webhook(feishu.webhook_url, title, message)
