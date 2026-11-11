from __future__ import annotations

from witty_service.runtime.contracts import RuntimeWorkspace
from witty_service.runtime.manager import WorkspaceRuntimeManager, WorkspaceRuntimeSpec
from witty_service.runtime.paths import WorkspaceRuntimePaths
from witty_service.sandbox.base import SandboxBackend
from witty_service.sandbox.factory import create_sandbox_backend

_OPENCODE_SERVE_ARGS = (
    "serve",
    "--hostname",
    "127.0.0.1",
    "--port",
    "{port}",
    "--print-logs",
)


def build_local_opencode_spec(
    *,
    workspace: RuntimeWorkspace,
    executable: str = "opencode",
    runtime_root: str | None = None,
    preferred_port: int | None = None,
) -> WorkspaceRuntimeSpec:
    """为本地 Workspace 构造 OpenCode 启动输入；Runtime Home 在这里落盘。"""
    paths = WorkspaceRuntimePaths.for_workspace(
        workspace.workspace_id, root=runtime_root
    )
    paths.ensure()
    env = paths.environment()
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    return WorkspaceRuntimeSpec(
        workspace=workspace,
        host_directory=workspace.runtime_directory,
        command=(executable, *_OPENCODE_SERVE_ARGS),
        env=env,
        preferred_port=preferred_port,
        stderr_log_path=str(paths.runtime_home / "opencode-serve.log"),
    )


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
