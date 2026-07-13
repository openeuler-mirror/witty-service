"""lifecycle service 共享的进程管理工具函数。"""

from __future__ import annotations

import logging
import socket
import threading
from subprocess import Popen

_STOP_TIMEOUT_SECONDS = 5
_KILL_TIMEOUT_SECONDS = 5
_STDERR_DRAIN_JOIN_TIMEOUT = 2


def port_is_listening(port: int) -> bool:
    """检测本地端口是否已处于监听状态。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except (OSError, ConnectionRefusedError):
        return False


def start_stderr_drainer(
    process: Popen[str],
    *,
    logger: logging.Logger,
    log_prefix: str,
    thread_name: str,
) -> threading.Thread | None:
    """启动 daemon thread 持续消费子进程 stderr，避免 PIPE 缓冲写满阻塞子进程。"""
    stderr = process.stderr
    if stderr is None:
        return None

    def _drain() -> None:
        try:
            for line in stderr:
                line = line.rstrip()
                if line:
                    logger.warning("%s: %s", log_prefix, line)
        except Exception:
            logger.debug("%s drainer exited", log_prefix, exc_info=True)

    drainer = threading.Thread(
        target=_drain,
        name=thread_name,
        daemon=True,
    )
    drainer.start()
    return drainer
