from __future__ import annotations

import logging
from threading import Lock


_CONFIG_LOCK = Lock()
_CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    global _CONFIGURED
    if not _CONFIGURED:
        with _CONFIG_LOCK:
            if not _CONFIGURED:
                logging.basicConfig(
                    level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                )
                _CONFIGURED = True
    return logging.getLogger(name)
