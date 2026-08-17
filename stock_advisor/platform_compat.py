"""跨平台兼容层：文件锁 + HTTP 抓取。

Linux/WSL 下原本依赖 fcntl.flock、curl、iconv 等命令。
Windows 原生没有这些，这里提供统一的跨平台替代：
- lock_file/unlock_file：文件锁（Linux 用 fcntl，Windows 用 msvcrt）
- http_get_bytes / http_get_text：用 requests 替代 curl（自动处理 GBK/UTF-8 编码）

所有需要跨平台运行的模块都从这里导入，保持业务逻辑不变。
"""
from __future__ import annotations

import os
import sys
import time

IS_WINDOWS = sys.platform == "win32"

# ═══════════════════════════════════════════════════════════════
# 文件锁
# ═══════════════════════════════════════════════════════════════

if IS_WINDOWS:
    import msvcrt

    def lock_file(fd: int) -> None:
        """阻塞式独占锁（锁定文件开头 1 字节）。"""
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.05)

    def unlock_file(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def lock_file(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def unlock_file(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


# ═══════════════════════════════════════════════════════════════
# HTTP 抓取（requests 替代 curl / curl|iconv）
# ═══════════════════════════════════════════════════════════════

_UA = "Mozilla/5.0"


def http_get_bytes(url: str, timeout: float = 10) -> bytes:
    """返回响应原始字节。任何异常返回 b''。"""
    try:
        import requests
        resp = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
        resp.raise_for_status()
        return resp.content
    except Exception:
        return b""


def http_get_text(
    url: str,
    timeout: float = 10,
    encoding: str | None = None,
) -> str:
    """返回响应文本。

    encoding 指定时按该编码解码；
    否则先尝试 UTF-8，失败回退 GBK（覆盖腾讯行情 GBK 场景）。
    任何异常返回空串。
    """
    data = http_get_bytes(url, timeout=timeout)
    if not data:
        return ""
    if encoding:
        return data.decode(encoding, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("gbk", errors="replace")
