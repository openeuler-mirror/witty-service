"""端口分配与占用探测。

沙箱自身端口（witty-agent-server）与 runtime 网关端口共用这一份实现，避免「复用判断」
在两处各写一遍后逐渐分叉。
"""

from __future__ import annotations

import socket

_DEFAULT_HOST = "127.0.0.1"


def find_free_port(host: str = _DEFAULT_HOST) -> int:
    """让内核分配一个当前空闲的端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def port_is_bindable(port: int, host: str = _DEFAULT_HOST) -> bool:
    """该端口现在还能不能 bind（用于决定是否复用已保存的端口）。

    必须带 SO_REUSEADDR：进程刚被停掉时内核会留下 FIN-WAIT-2 / TIME-WAIT 的残留
    socket，裸 bind() 会因此报 EADDRINUSE，把「刚停掉、马上要复用」误判成「端口被
    占」，于是每次恢复都白换一个新端口。SO_REUSEADDR 只放宽这类残留 socket：真有
    进程在 LISTEN 时依旧 EADDRINUSE，所以「真占用」仍然判得出来。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False
