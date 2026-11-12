from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from witty_service.domain.errors import DomainError

SANDBOX_NOT_FOUND = "SANDBOX_NOT_FOUND"
SANDBOX_START_FAILED = "SANDBOX_START_FAILED"
SANDBOX_STOP_FAILED = "SANDBOX_STOP_FAILED"
SANDBOX_NOT_SUPPORTED = "SANDBOX_NOT_SUPPORTED"


class SandboxStatus(StrEnum):
    starting = "starting"
    running = "running"
    stopped = "stopped"
    error = "error"


@dataclass(slots=True, frozen=True)
class SandboxHandle:
    sandbox_id: str
    workspace_id: str
    workspace_path: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class AdapterEndpoint:
    base_url: str
    health_url: str | None = None

    @property
    def ws_url(self) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://")[-1]
        return f"{scheme}://{host}/agent/sessions/{{session_id}}/ws"

    def ws_endpoint(self, session_id: str) -> str:
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://")[-1]
        return f"{scheme}://{host}/agent/sessions/{session_id}/ws"


class SandboxBackend(ABC):
    sandbox_type: str = "unknown"

    @abstractmethod
    def start(
        self,
        *,
        workspace_id: str,
        workspace_path: str,
        env: dict[str, str] | None = None,
        replace_env: bool = False,
        **kwargs: Any,
    ) -> SandboxHandle:
        """启动 Workspace 执行体。

        ``replace_env=True`` 表示 ``env`` 是完整环境，不透传宿主进程环境；
        Runtime Home 的 HOME/XDG 隔离依赖该语义。
        """
        raise NotImplementedError

    @abstractmethod
    def stop(self, handle: SandboxHandle | str, **kwargs: Any) -> None:
        raise NotImplementedError

    @abstractmethod
    def status(self, handle: SandboxHandle | str, **kwargs: Any) -> SandboxStatus:
        raise NotImplementedError

    @abstractmethod
    def endpoint(self, handle: SandboxHandle | str, **kwargs: Any) -> AdapterEndpoint:
        raise NotImplementedError

    @abstractmethod
    def cleanup(self, handle: SandboxHandle | str, **kwargs: Any) -> None:
        raise NotImplementedError

    def write_runtime_files(
        self, *, runtime_home: str, files: Mapping[str, str]
    ) -> None:
        """原子写入 Runtime Home 内的配置；Runtime Home 的磁盘 I/O 只走沙箱。"""
        raise sandbox_not_supported(
            sandbox_type=self.sandbox_type, operation="write_runtime_files"
        )

    def stop_all(self, **kwargs: Any) -> None:
        """停止本 backend 记录的所有沙箱（服务关停时调用）。

        默认空实现：只有「沙箱进程随 witty-service 生命周期」的 backend 才需要
        覆盖它——漏掉这些进程会直接转化为端口占用与内存泄漏。
        """
        return None

    def find_handle_by_workspace(
        self, *, workspace_id: str, workspace_path: str
    ) -> SandboxHandle | None:
        """按 agent 身份回查仍在运行的执行体句柄。

        DB 里没有 sandbox_state（创建中途）时用于删除前的安全检查。默认实现无法
        证明执行体不存在，直接抛错（unknown），由调用方阻断删除；能可靠回查的
        backend 必须覆盖本方法。

        返回 None 只表示已确证不存在；查询失败必须抛错。
        """
        raise NotImplementedError(
            f"{type(self).__name__} cannot resolve sandbox handle by agent identity"
        )


def sandbox_not_supported(*, sandbox_type: str, operation: str) -> DomainError:
    return DomainError(
        code=SANDBOX_NOT_SUPPORTED,
        message="Sandbox backend is not supported yet.",
        status_code=400,
        details={
            "sandbox_type": sandbox_type,
            "operation": operation,
        },
    )


def sandbox_not_found(*, sandbox_type: str, sandbox_id: str) -> DomainError:
    return DomainError(
        code=SANDBOX_NOT_FOUND,
        message="Sandbox handle was not found.",
        status_code=404,
        details={
            "sandbox_type": sandbox_type,
            "sandbox_id": sandbox_id,
        },
    )


def sandbox_start_failed(
    *,
    sandbox_type: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> DomainError:
    payload = {
        "sandbox_type": sandbox_type,
    }
    payload.update(details or {})
    return DomainError(
        code=SANDBOX_START_FAILED,
        message=message,
        status_code=500,
        details=payload,
    )


def sandbox_stop_failed(
    *,
    sandbox_type: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> DomainError:
    payload = {
        "sandbox_type": sandbox_type,
    }
    payload.update(details or {})
    return DomainError(
        code=SANDBOX_STOP_FAILED,
        message=message,
        status_code=500,
        details=payload,
    )
