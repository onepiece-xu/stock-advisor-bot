from __future__ import annotations

import requests


def send_feishu_webhook(webhook_url: str, title: str, message: str) -> None:
    payload = {
        "msg_type": "text",
        "content": {
            "text": f"{title}\n\n{message}"
        },
    }
    response = requests.post(webhook_url, json=payload, timeout=8)
    response.raise_for_status()
