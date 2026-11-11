from __future__ import annotations

from witty_service.runtime.manager import WorkspaceRuntimeManager
from witty_service.sandbox.base import SandboxBackend
from witty_service.sandbox.factory import create_sandbox_backend


def build_workspace_runtime_manager(
    *,
    sandbox_type: str = "local_process",
    sandbox: SandboxBackend | None = None,
) -> WorkspaceRuntimeManager:
    """组合根：控制面只通过这里接触执行面实现。"""
    from witty_agent_server.runtime_adapter import OpenCodeRuntimeAdapter

    return WorkspaceRuntimeManager(
        port=OpenCodeRuntimeAdapter(),
        sandbox=sandbox
        if sandbox is not None
        else create_sandbox_backend(sandbox_type),
    )
