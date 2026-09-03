from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence
from pathlib import Path

from deepseek_harness.errors import HarnessError

from witty_agent_server.infra.clients.dsh_client import DshClient
from witty_service.config import get_settings
from witty_service.workspace_paths import agent_workspace_path, validate_agent_id

logger = logging.getLogger(__name__)


class DshLifecycleError(Exception):
    """dsh 生命周期控制错误。"""

    def __init__(self, *, action: str, message: str) -> None:
        super().__init__(message)
        self.action = action
        self.message = message


class DshLifecycleService:
    """DeepSeek Harness（dsh）生命周期控制。

    管理 ``DeepSeekHarness`` 对象（子进程由 SDK 拥有），配置由
    ``DshClient`` 持有（update_config 的 detach 语义即「变更即重建」）：
    推导/准备实例目录、start_server（ensure + start）、probe_running
    （握手完成且子进程存活）、stop（close 后复查进程存活）。
    """

    def __init__(self, client: DshClient) -> None:
        self._client = client
        self._agent_id: str | None = None

    @property
    def client(self) -> DshClient:
        return self._client

    def update_config(
        self,
        *,
        agent_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        """记录新配置；任一字段真实变更时由 client detach 旧 harness。

        ``initialize`` 是实例级一次性握手，配置变更即需重建 harness——
        detach 语义即「若在运行则 stop，下次 start 用新配置」。
        """
        if agent_id:
            try:
                validate_agent_id(agent_id)
            except ValueError as exc:
                raise DshLifecycleError(
                    action="update_config",
                    message=f"invalid agent_id {agent_id!r}: {exc}",
                ) from exc
            if agent_id != self._agent_id:
                # 切换 agent：先复位模型配置，防止上一 agent 的凭据串用。
                self._client.reset_model_config()
            self._agent_id = agent_id
        paths = self._derive_instance_paths()
        workspace_dir = str(paths[0]) if paths else None
        session_root = str(paths[1]) if paths else None
        self._client.update_config(
            workspace_dir=workspace_dir,
            session_root=session_root,
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_tokens=max_tokens,
        )

    def start_server(self) -> None:
        """启动 dsh harness：准备实例目录 → 清理僵尸 harness → ensure → start。"""
        paths = self._derive_instance_paths()
        if paths is not None:
            workspace_dir, session_root = paths
            try:
                workspace_dir.mkdir(parents=True, exist_ok=True)
                session_root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise DshLifecycleError(
                    action="start",
                    message=(
                        f"failed to create dsh instance dirs for "
                        f"agent={self._agent_id}: {exc}"
                    ),
                ) from exc
            # 幂等下推（值未变时 client 不 detach）
            self._client.update_config(
                workspace_dir=str(workspace_dir),
                session_root=str(session_root),
            )

        # 僵尸/崩溃 harness 无法复用（start 对已初始化 harness 是 no-op），丢弃重建。
        harness = self._client.harness
        if harness is not None and self._needs_rebuild(harness):
            logger.warning(
                "dsh harness is initialized but its subprocess is dead; "
                "discarding and rebuilding (agent=%s)",
                self._agent_id,
            )
            self._client.close_harness()

        try:
            harness = self._client.ensure_harness()
            harness.start()
        except (HarnessError, TimeoutError, OSError) as exc:
            # start 失败后 harness 状态未知，关闭丢弃，避免 probe 误判存活。
            self._client.close_harness()
            raise DshLifecycleError(
                action="start",
                message=f"dsh harness start failed: {exc}",
            ) from exc

    def probe_running(self) -> bool:
        """harness 已完成 initialize 握手且底层子进程存活。"""
        harness = self._client.harness
        return harness is not None and self._is_harness_alive(harness)

    @staticmethod
    def _harness_proc(harness: object) -> subprocess.Popen[str] | None:
        """收敛 SDK 私有属性访问：返回底层子进程对象（未启动/无进程时为 None）。"""
        client = getattr(harness, "client", None)
        return getattr(client, "_proc", None)

    @staticmethod
    def _is_harness_alive(harness: object) -> bool:
        """已握手且底层子进程存活（协议无 ping 方法，以进程存活为准）。"""
        if not getattr(harness, "_initialized", False):
            return False
        proc = DshLifecycleService._harness_proc(harness)
        return proc is not None and proc.poll() is None

    @staticmethod
    def _needs_rebuild(harness: object) -> bool:
        """已握手但子进程已退出：SDK start() 对已初始化 harness 是 no-op，需丢弃重建。"""
        return getattr(harness, "_initialized", False) and not (
            DshLifecycleService._is_harness_alive(harness)
        )

    def stop(self) -> None:
        """关闭 harness；复查底层进程存活，仍存活则抛 DshLifecycleError。

        不变量：正常返回时子进程必须已终止——close 为 best-effort，
        直读 ``poll()`` 复查（不依赖 close 时重置的 ``_initialized``），
        避免僵尸进程泄漏且状态误报 STOPPED。
        """
        harness = self._client.harness
        self._client.close_harness()
        if harness is not None:
            proc = self._harness_proc(harness)
            if proc is not None and proc.poll() is None:
                raise DshLifecycleError(
                    action="stop",
                    message="dsh harness still alive after close",
                )

    def _derive_instance_paths(self) -> tuple[Path, Path] | None:
        """按 agent_id 推导 dsh 实例目录（workspace / sessions）。

        ``workspace`` 与 ``agent.workspace_path``（单一事实来源
        ``agent_workspace_path``）同源，使 dsh ``write`` 工具产物落入
        artifact 归一化 / 工作区文件端点所校验的同一工作区；``sessions``
        （会话 JSONL 转录）属运行时状态，与 opencode 的 data/state/cache
        同理，不进入 AI 工作区，仍按 dsh 实例目录隔离。
        """
        if not self._agent_id:
            return None
        workspace = agent_workspace_path(
            self._agent_id, root=get_settings().workspace.root_path()
        )
        session_root = (
            get_settings().workspace.root_path()
            / "dsh-instances"
            / self._agent_id
            / "sessions"
        )
        return workspace, session_root


__all__: Sequence[str] = ("DshLifecycleError", "DshLifecycleService")
