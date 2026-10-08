from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

from witty_service.sandbox.base import (
    AdapterEndpoint,
    SandboxBackend,
    SandboxHandle,
    SandboxStatus,
    sandbox_not_found,
    sandbox_start_failed,
    sandbox_stop_failed,
)
from witty_service.sandbox.ports import find_free_port, port_is_bindable


def read_proc_stat(pid: int) -> tuple[str, str, str] | None:
    """读取 ``/proc/<pid>/stat``，返回 ``(状态字符, 进程组 id, 启动时间)``。

    进程不存在 / 无权限时返回 ``None``。启动时间（proc(5) 第 22 个字段）在同一台
    机器上不会重复，因此它是判断 *pid 是否被复用* 的唯一可靠依据——只凭 pid 存活
    就认领/清理一个进程，迟早会杀掉别人的进程。
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # comm（第 2 个字段）可能包含空格和括号，按最后一个右括号切分才能稳定取到后续字段。
    _, sep, rest = raw.rpartition(")")
    if not sep:
        return None
    fields = rest.split()
    if len(fields) < 20:
        return None
    # rest 的第一个 token 是第 3 个字段(state)，故 pgrp(第 5 个) 下标为 2、
    # starttime(第 22 个) 下标为 19。
    return fields[0], fields[2], fields[19]


def read_proc_cmdline(pid: int) -> list[str] | None:
    """读取 ``/proc/<pid>/cmdline``；进程不存在 / 无权限时返回 ``None``。"""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    if not raw:  # 僵尸进程的 cmdline 为空
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


class LocalProcessSandboxBackend(SandboxBackend):
    sandbox_type = "local_process"

    # ws keepalive 参数，见 _build_command 里的说明。
    ws_ping_interval_seconds: float = 20.0
    ws_ping_timeout_seconds: float = 120.0

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        agent_server_app_dir: str | None = None,
        stop_timeout: float = 5.0,
        startup_poll_interval: float = 0.1,
        ws_ping_interval_seconds: float | None = None,
        ws_ping_timeout_seconds: float | None = None,
    ) -> None:
        self.host = host
        self.agent_server_app_dir = agent_server_app_dir
        self.stop_timeout = stop_timeout
        self.startup_poll_interval = startup_poll_interval
        if ws_ping_interval_seconds is not None:
            self.ws_ping_interval_seconds = ws_ping_interval_seconds
        if ws_ping_timeout_seconds is not None:
            self.ws_ping_timeout_seconds = ws_ping_timeout_seconds
        self._handles: dict[str, SandboxHandle] = {}
        self._processes: dict[str, Any] = {}

    def start(
        self,
        *,
        agent_id: str,
        workspace_path: str,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> SandboxHandle:
        """启动本地 witty-agent-server 子进程并返回沙箱句柄。

        ``previous_handle``：该 agent 上一次的沙箱句柄（来自数据库）。若它已经不在本
        进程的登记表里（服务重启）或已经被判死（探活失败），先按元数据验证身份把旧
        进程树收敛掉再新建。否则每恢复一次就多留一棵孤儿进程树，它们占着端口，正是
        后续恢复失败的直接原因。
        """
        logger.info(f"[LocalProcessSandbox] Starting sandbox for agent_id: {agent_id}")
        previous_handle = kwargs.get("previous_handle")
        if isinstance(previous_handle, SandboxHandle):
            # 兜底校验归属：句柄串号时宁可多留一个孤儿，也不能误杀别的 agent。
            if previous_handle.agent_id == agent_id:
                self._stop_previous_sandbox(previous_handle)
            else:
                logger.warning(
                    "[LocalProcessSandbox] Ignore previous_handle of another agent: "
                    "expected=%s got=%s",
                    agent_id,
                    previous_handle.agent_id,
                )
        # 端口优先复用调用方记录的上一次取值（旧进程树此时已收敛）；只有真的被别的
        # 进程占着才退回随机端口——否则每次重启/恢复都会把沙箱端口换掉。
        preferred_port = kwargs.get("port")
        if preferred_port and port_is_bindable(int(preferred_port)):
            port = int(preferred_port)
        else:
            port = find_free_port()
        logger.info(f"[LocalProcessSandbox] Using port: {port}")
        app_dir = self._resolve_agent_server_app_dir()
        logger.info(f"[LocalProcessSandbox] Agent server app dir: {app_dir}")
        command = self._build_command(port=port, app_dir=app_dir)
        logger.info(f"[LocalProcessSandbox] Command: {' '.join(command)}")
        stderr_log_path = self._build_stderr_log_path(workspace_path)
        stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"[LocalProcessSandbox] Stderr log path: {stderr_log_path}")

        # 透传调用方注入的额外环境变量（例如 WITTY_RUNTIME_DEFAULT），
        env_vars = os.environ.copy()
        if env:
            env_vars.update(env)
            logger.info(
                "[LocalProcessSandbox] Merging extra env vars: %s",
                list(env.keys()),
            )

        try:
            logger.info(f"[LocalProcessSandbox] Starting process in cwd: {command}")
            logger.info(f"[LocalProcessSandbox] Workspace path: {workspace_path}")
            with stderr_log_path.open("a", encoding="utf-8") as stderr_file:
                process = subprocess.Popen(
                    command,
                    cwd=workspace_path,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    env=env_vars,
                    text=True,
                    # 自成会话/进程组：沙箱整棵树（agent server + 它拉起的 opencode
                    # serve + MCP 子进程）都在同一个组里，停止时才能一起收敛。
                    start_new_session=True,
                )
            logger.info(
                f"[LocalProcessSandbox] Process started with PID: {process.pid} in cwd: {app_dir}"
            )
        except OSError as exc:
            logger.error(f"[LocalProcessSandbox] Failed to start process: {exc}")
            raise sandbox_start_failed(
                sandbox_type=self.sandbox_type,
                message="Failed to start local process sandbox.",
                details={
                    "command": command,
                    "stderr": str(exc),
                },
            ) from exc

        time.sleep(self.startup_poll_interval)
        returncode = process.poll()
        logger.info(f"[LocalProcessSandbox] Initial poll returncode: {returncode}")
        if returncode is not None:
            stderr = self._read_stderr_log(stderr_log_path)
            logger.error(
                f"[LocalProcessSandbox] Process exited immediately: returncode={returncode}, stderr={stderr}"
            )
            raise sandbox_start_failed(
                sandbox_type=self.sandbox_type,
                message="Local process sandbox exited immediately after startup.",
                details={
                    "command": command,
                    "stderr": stderr,
                    "returncode": returncode,
                },
            )

        sandbox_id = str(uuid4())
        base_url = f"http://{self.host}:{port}"
        logger.info(
            f"[LocalProcessSandbox] Sandbox ID: {sandbox_id}, base_url: {base_url}"
        )
        handle = SandboxHandle(
            sandbox_id=sandbox_id,
            agent_id=agent_id,
            workspace_path=workspace_path,
            metadata={
                "pid": process.pid,
                # pgid == pid：本进程即组长（start_new_session=True）。stop() 只在
                # 确证这一点时才按进程组下发信号，绝不盲目 killpg。
                "pgid": process.pid,
                # 启动时间用于识别 pid 复用，跨服务重启清理残留进程时必需。
                "process_start_time": self._read_process_start_time(process.pid),
                "port": port,
                "base_url": base_url,
                "command": command,
                "agent_server_app_dir": app_dir,
                "stderr_log_path": str(stderr_log_path),
            },
        )
        self._handles[sandbox_id] = handle
        self._processes[sandbox_id] = process
        logger.info(
            "[LocalProcessSandbox] Sandbox started successfully, returning handle"
        )
        return handle

    def stop(self, handle: SandboxHandle | str, **kwargs: Any) -> None:
        """停止沙箱的**整棵进程树**（agent server + opencode serve + MCP 子进程）。"""
        sandbox_handle = self._resolve_handle(handle)
        process = self._processes.get(sandbox_handle.sandbox_id)
        if process is None:
            self._reap_untracked_sandbox(sandbox_handle)
            return

        # 必须在回收僵尸**之前**解析 pgid：进程被 wait() 回收后 /proc 条目就没了。
        pgid = self._verified_process_group(sandbox_handle)
        if pgid is None:
            logger.warning(
                "[LocalProcessSandbox] Cannot verify process group for pid=%s "
                "(sandbox_id=%s); falling back to leader-only stop",
                sandbox_handle.metadata.get("pid"),
                sandbox_handle.sandbox_id,
            )

        if process.poll() is None:
            if pgid is not None:
                self._signal_process_group(pgid, signal.SIGTERM)
            process.terminate()
            try:
                process.wait(timeout=self.stop_timeout)
            except subprocess.TimeoutExpired:
                if pgid is not None:
                    self._signal_process_group(pgid, signal.SIGKILL)
                process.kill()
                process.wait(timeout=self.stop_timeout)
        else:
            # 组长已经退出：先回收僵尸，否则僵尸成员会让「进程组是否存活」永远为真。
            process.wait()

        if pgid is not None:
            self._reap_process_group(pgid, timeout=self.stop_timeout)

    def stop_all(self, **kwargs: Any) -> None:
        """停止本 backend 记录的所有沙箱（服务关停时调用，避免留下孤儿）。"""
        for sandbox_id in list(self._handles):
            handle = self._handles[sandbox_id]
            try:
                self.cleanup(handle, **kwargs)
            except Exception:
                logger.exception(
                    "[LocalProcessSandbox] Failed to stop sandbox on shutdown: "
                    "sandbox_id=%s pid=%s",
                    sandbox_id,
                    handle.metadata.get("pid"),
                )
        self._handles.clear()
        self._processes.clear()

    def status(self, handle: SandboxHandle | str, **kwargs: Any) -> SandboxStatus:
        sandbox_handle = self._resolve_handle(handle)
        process = self._processes.get(sandbox_handle.sandbox_id)
        if process is None:
            return SandboxStatus.stopped
        if process.poll() is None:
            return SandboxStatus.running
        return SandboxStatus.stopped

    def endpoint(self, handle: SandboxHandle | str, **kwargs: Any) -> AdapterEndpoint:
        sandbox_handle = self._resolve_handle(handle)
        base_url = str(sandbox_handle.metadata["base_url"])
        return AdapterEndpoint(base_url=base_url, health_url=f"{base_url}/ping")

    def cleanup(self, handle: SandboxHandle | str, **kwargs: Any) -> None:
        sandbox_handle = self._resolve_handle(handle)
        self.stop(sandbox_handle, **kwargs)
        self._processes.pop(sandbox_handle.sandbox_id, None)
        self._handles.pop(sandbox_handle.sandbox_id, None)

    # ======================================================================
    # 进程树收敛：按进程组停止，并能在服务重启后清理残留
    # ======================================================================

    @staticmethod
    def _read_process_start_time(pid: int) -> str | None:
        """读取进程启动时间（写入句柄元数据，用于日后识别 pid 复用）。"""
        stat = read_proc_stat(pid)
        return stat[2] if stat is not None else None

    def _pid_is_our_agent_server(self, pid: int, metadata: dict[str, Any]) -> bool:
        """确认 *pid* 现在仍然是本沙箱的 witty-agent-server 进程。

        三重校验缺一不可：进程存在、启动时间与记录一致（排除 pid 复用）、命令行
        里带本沙箱的监听端口（排除「这个 pid 是别人的」）。任何一条对不上都返回
        ``False``——宁可留着孤儿也不误杀无关进程。
        """
        stat = read_proc_stat(pid)
        if stat is None:
            return False
        state, _pgrp, start_time = stat
        recorded_start_time = metadata.get("process_start_time")
        if isinstance(recorded_start_time, str) and recorded_start_time:
            if start_time != recorded_start_time:
                return False
            if state == "Z":
                # 僵尸进程读不到 cmdline，但 starttime 已经证明了身份。
                return True
        cmdline = read_proc_cmdline(pid)
        if not cmdline:
            return False
        if "witty_agent_server.app:create_app" not in cmdline:
            return False
        port = metadata.get("port")
        return port is None or self._port_in_cmdline(cmdline, port)

    @staticmethod
    def _port_in_cmdline(cmdline: list[str], port: Any) -> bool:
        """命令行里是否出现 ``--port <port>``。"""
        expected = str(port)
        for index, arg in enumerate(cmdline):
            if arg == "--port" and index + 1 < len(cmdline):
                return cmdline[index + 1] == expected
        return False

    def _verified_process_group(self, handle: SandboxHandle) -> int | None:
        """能确证「该沙箱自成进程组」时返回 pgid，否则返回 ``None``。

        只认 ``pgid == pid``：满足它才说明这个 pid 是组长（``start_new_session=True``
        的结果），对它 killpg 只会影响它自己的进程树。旧版本句柄没有 pgid 字段，
        或者 pid 已被系统复用，都会走到 ``None`` 分支，由调用方退回单进程处理。
        """
        metadata = handle.metadata
        pid = metadata.get("pid")
        pgid = metadata.get("pgid")
        if not isinstance(pid, int) or not isinstance(pgid, int) or pgid != pid:
            return None
        if not self._pid_is_our_agent_server(pid, metadata):
            return None
        return pgid

    @staticmethod
    def _signal_process_group(pgid: int, sig: int) -> bool:
        """给进程组下发信号；组已不存在返回 ``False``。"""
        try:
            os.killpg(pgid, sig)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            logger.warning(
                "[LocalProcessSandbox] No permission to signal process group %s", pgid
            )
            return False

    @staticmethod
    def _process_group_exists(pgid: int) -> bool:
        """进程组里是否还有**活着的**成员。

        僵尸成员也让 ``killpg(pgid, 0)`` 成功，但僵尸既不能再干活也不会释放端口，
        而且杀不掉——把它当成存活会让 stop 等满超时后误报失败。
        """
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return LocalProcessSandboxBackend._group_has_live_member(pgid)

    @staticmethod
    def _group_has_live_member(pgid: int) -> bool:
        try:
            entries = os.listdir("/proc")
        except OSError:
            return False
        for entry in entries:
            if not entry.isdigit():
                continue
            stat = read_proc_stat(int(entry))
            if stat is None:
                continue
            state, pgrp, _start_time = stat
            if pgrp == str(pgid) and state != "Z":
                return True
        return False

    @staticmethod
    def _process_exists(pid: int) -> bool:
        """进程是否存在且尚未变成僵尸。"""
        stat = read_proc_stat(pid)
        return stat is not None and stat[0] != "Z"

    @staticmethod
    def _wait_until(
        predicate: Callable[[], bool], timeout: float, *, interval: float = 0.05
    ) -> bool:
        """轮询 *predicate* 直到为真或超时（返回是否在超时前满足）。"""
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            if predicate():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)

    def _reap_process_group(self, pgid: int, *, timeout: float) -> None:
        """确保整个进程组退出：SIGTERM → SIGKILL；仍有存活则显式报错。

        组长退出不等于整棵树退出：``opencode serve`` 是 agent server 的子进程，
        agent server 被 SIGTERM 后它会留在同一个进程组里继续占端口。
        """
        self._signal_process_group(pgid, signal.SIGTERM)
        # 判定组内是否还有活着的成员需要遍历 /proc，轮询间隔放宽到 0.1s。
        if self._wait_until(
            lambda: not self._process_group_exists(pgid), timeout, interval=0.1
        ):
            return
        logger.warning(
            "[LocalProcessSandbox] Process group %s survived SIGTERM, sending SIGKILL",
            pgid,
        )
        self._signal_process_group(pgid, signal.SIGKILL)
        if not self._wait_until(
            lambda: not self._process_group_exists(pgid), timeout, interval=0.1
        ):
            raise sandbox_stop_failed(
                sandbox_type=self.sandbox_type,
                message="Local process sandbox survived stop signals.",
                details={"pgid": pgid},
            )

    def _stop_previous_sandbox(self, handle: SandboxHandle) -> None:
        """收敛上一次的沙箱：本进程登记过就正常停止，否则按元数据清理残留。"""
        if handle.sandbox_id in self._processes:
            self.cleanup(handle)
        else:
            self._reap_untracked_sandbox(handle)

    def _reap_untracked_sandbox(self, handle: SandboxHandle) -> None:
        """清理不属于本进程的残留沙箱（服务重启后按数据库里的句柄收敛）。"""
        metadata = handle.metadata
        pid = metadata.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return

        pgid = self._verified_process_group(handle)
        if pgid is not None:
            logger.info(
                "[LocalProcessSandbox] Reaping orphan process group %s (agent_id=%s)",
                pgid,
                handle.agent_id,
            )
            self._reap_process_group(pgid, timeout=self.stop_timeout)
            return

        if not self._pid_is_our_agent_server(pid, metadata):
            logger.warning(
                "[LocalProcessSandbox] Skip reaping pid=%s for agent_id=%s: "
                "process is gone or is not our agent server",
                pid,
                handle.agent_id,
            )
            return

        # 旧版本句柄没记 pgid：只能杀 leader，opencode serve 可能残留。
        logger.warning(
            "[LocalProcessSandbox] Reaping pid=%s without process group metadata", pid
        )
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        if self._wait_until(lambda: not self._process_exists(pid), self.stop_timeout):
            return
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        if not self._wait_until(
            lambda: not self._process_exists(pid), self.stop_timeout
        ):
            raise sandbox_stop_failed(
                sandbox_type=self.sandbox_type,
                message="Local process sandbox survived stop signals.",
                details={"pid": pid},
            )

    def reap_orphan_agent_servers(
        self,
        *,
        registered: dict[int, str],
        workspace_root: Path,
    ) -> list[int]:
        """扫描 ``/proc``，收敛游离的 witty-agent-server 进程树。

        服务重启、异常退出或手动测试会在系统里留下不属于任何 DB 登记 agent 的
        agent-server 进程（及其 runtime 子进程），白白占用端口和内存。本方法在
        服务启动时调用，把这些孤儿按进程组收敛掉。

        豁免条件（命中其一即跳过）：

        - ``(pid, process_start_time)`` 与 *registered* 一致——DB 登记在案的沙箱
          （startup 恢复流程会接管它）；
        - 进程 cwd 不在 *workspace_root* 之下——属于其它 witty-service 实例
          （不同 WITTY_WORKSPACE_ROOT），不能误杀。

        返回被收敛的 leader pid 列表。
        """
        reaped: list[int] = []
        root = Path(workspace_root).expanduser().resolve()
        try:
            entries = os.listdir("/proc")
        except OSError:
            return reaped
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            cmdline = read_proc_cmdline(pid)
            if not cmdline or "witty_agent_server.app:create_app" not in cmdline:
                continue
            stat = read_proc_stat(pid)
            if stat is None:
                continue
            _state, pgrp, start_time = stat
            if start_time is not None and registered.get(pid) == start_time:
                continue
            if not self._cwd_under(pid, root):
                logger.info(
                    "[LocalProcessSandbox] Skip pid=%s: cwd is outside workspace root",
                    pid,
                )
                continue
            logger.warning(
                "[LocalProcessSandbox] Reaping orphan agent server pid=%s "
                "(not registered in DB)",
                pid,
            )
            # 先快照后代：leader 死后子进程会被 init 收养，届时无从追踪。
            # 正常路径（backend 自行拉起、start_new_session 自成会话）整组收敛即可
            # 覆盖；手动/异常启动的树可能各成进程组，需逐个补杀。
            descendants = self._collect_descendants(pid)
            if pgrp == str(pid):
                self._reap_process_group(int(pgrp), timeout=self.stop_timeout)
            else:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGTERM)
                if self._process_exists(pid):
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(pid, signal.SIGKILL)
            for child in descendants:
                child_stat = read_proc_stat(child)
                child_pgrp = child_stat[1] if child_stat is not None else None
                if child_pgrp == str(child):
                    self._reap_process_group(int(child_pgrp), timeout=self.stop_timeout)
                else:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(child, signal.SIGTERM)
                    if self._process_exists(child):
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            os.kill(child, signal.SIGKILL)
            reaped.append(pid)
        return reaped

    @staticmethod
    def _collect_descendants(pid: int) -> list[int]:
        """收集 *pid* 的全部后代（必须在 leader 被杀之前调用）。

        杀掉 leader 后子进程会被 init 收养（ppid 变 1），与父进程的关联就此丢失，
        因此先按 ppid 关系做一次 BFS 快照。
        """
        children: dict[int, list[int]] = {}
        try:
            entries = os.listdir("/proc")
        except OSError:
            return []
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                raw = Path(f"/proc/{entry}/stat").read_text(encoding="utf-8")
            except OSError:
                continue
            _, sep, rest = raw.rpartition(")")
            if not sep:
                continue
            fields = rest.split()
            if len(fields) < 2:
                continue
            try:
                ppid = int(fields[1])
            except ValueError:
                continue
            children.setdefault(ppid, []).append(int(entry))
        result: list[int] = []
        queue = [pid]
        while queue:
            current = queue.pop()
            for child in children.get(current, []):
                result.append(child)
                queue.append(child)
        return result

    @staticmethod
    def _cwd_under(pid: int, root: Path) -> bool:
        """进程的 cwd 是否位于 *root* 目录树之内（判归属的关键依据）。"""
        try:
            cwd = Path(os.readlink(f"/proc/{pid}/cwd")).resolve()
        except OSError:
            return False
        return cwd == root or root in cwd.parents

    def _build_command(self, *, port: int, app_dir: str) -> list[str]:
        """构造 witty-agent-server 的启动命令。"""
        witty_service_dir = str(Path(app_dir).parent)

        # ws keepalive：uvicorn 默认 ping_interval=20s / ping_timeout=20s，即"20 秒内
        # 收不到 pong 就掐连接(1011)"。消费端是 witty-service 的 AgentManager，与本
        # 进程同机；当它因为落库/GC/机器负载一时回不过神，默认值就会把一次正常的
        # 长任务判死（表现为前端"任务突然变 error"）。这里放宽超时——真正的断连由
        # 对端主动 close / TCP RST 立刻发现，不依赖这个心跳超时。
        # 注意：这是**兜底**，根因（同步落库堵死事件循环）在 witty-service 侧修复，
        # 见 persistence.db._configure_sqlite_engine 与 agent_manager.PERSIST_BATCH_*。
        return [
            sys.executable,
            "-m",
            "uvicorn",
            "witty_agent_server.app:create_app",
            "--factory",
            "--app-dir",
            witty_service_dir,
            "--host",
            self.host,
            "--port",
            str(port),
            "--ws-ping-interval",
            str(self.ws_ping_interval_seconds),
            "--ws-ping-timeout",
            str(self.ws_ping_timeout_seconds),
        ]

    def _resolve_agent_server_app_dir(self) -> str:
        # 优先使用显式配置的目录
        if self.agent_server_app_dir:
            path = Path(self.agent_server_app_dir).expanduser().resolve(strict=False)
            if path.is_dir():
                return str(path)

        # 自动检测：从当前文件所在位置推断项目根目录
        # 当前文件路径: /root/new/witty-service/src/sandbox/local_process.py
        # 项目根目录: /root/new/witty-service
        # witty_agent_server 目录: /root/new/witty-service/witty_agent_server
        current_file = Path(__file__).resolve()
        project_root = (
            current_file.parent.parent.parent
        )  # src/sandbox -> src -> project_root
        witty_agent_server_dir = project_root / "witty_agent_server"

        if witty_agent_server_dir.is_dir():
            return str(witty_agent_server_dir)

        raise sandbox_start_failed(
            sandbox_type=self.sandbox_type,
            message=(
                "Cannot find witty_agent_server directory. "
                "Please set WITTY_AGENT_SERVER_APP_DIR environment variable "
                "or ensure witty_agent_server exists in the project root."
            ),
            details={
                "searched_path": str(witty_agent_server_dir),
                "env_var": "WITTY_AGENT_SERVER_APP_DIR",
            },
        )

    @staticmethod
    def _build_stderr_log_path(workspace_path: str) -> Path:
        """为本地子进程生成固定的 stderr 日志文件路径。"""
        logger.info(f"Building stderr log path for workspace: {workspace_path}")
        return (
            Path(workspace_path).expanduser().resolve(strict=False)
            / "agent-server.stderr.log"
        )

    def _resolve_handle(self, handle: SandboxHandle | str) -> SandboxHandle:
        if isinstance(handle, SandboxHandle):
            # 句柄自带全部元数据即可操作。服务重启后 self._handles 是空的，但数据库
            # 里保存的句柄仍然要能被 stop / cleanup——否则残留进程永远清不掉。
            return handle
        try:
            return self._handles[handle]
        except KeyError as exc:
            raise sandbox_not_found(
                sandbox_type=self.sandbox_type,
                sandbox_id=handle,
            ) from exc

    @staticmethod
    def _read_stderr_log(stderr_log_path: Path) -> str:
        """读取启动失败时写入的 stderr 日志，便于错误排查。"""
        if not stderr_log_path.exists():
            return ""
        return stderr_log_path.read_text(encoding="utf-8")
