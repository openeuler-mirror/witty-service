from __future__ import annotations

import shutil
from pathlib import Path

from witty_service.workspace_paths import (
    agent_root_path,
    agent_workspace_path,
    validate_agent_id,
)

#: runtime 实例隔离目录名（与 witty_agent_server 侧创建逻辑保持一致）。
RUNTIME_INSTANCE_DIR_NAMES = ("opencode-instances",)


class WorkspaceStore:
    def __init__(
        self,
        base_dir: str | Path | None = None,
        *,
        base_path: str | Path | None = None,
    ) -> None:
        if base_dir is None and base_path is None:
            raise TypeError("WorkspaceStore requires base_dir or base_path")
        if (
            base_dir is not None
            and base_path is not None
            and Path(base_dir) != Path(base_path)
        ):
            raise ValueError("base_dir and base_path must refer to the same path")

        if base_dir is not None:
            self.base_dir = Path(base_dir).expanduser().resolve()
        else:
            self.base_dir = Path(base_path).expanduser().resolve()

    def init_workspace(self, agent_id: str) -> Path:
        workspace_path = self._agent_workspace_path(agent_id)
        for relative_path in (".agent", "code", "input", "output"):
            (workspace_path / relative_path).mkdir(parents=True, exist_ok=True)
        return workspace_path

    def cleanup_workspace(self, agent_id: str) -> None:
        """删除 agent 的整个磁盘目录（含 workspace 及同级文件，如 stderr 日志）。"""
        agent_root = agent_root_path(agent_id, root=self.base_dir)
        self._ensure_managed_target(
            agent_root, allowed_parent=self.base_dir / "agent-workspaces"
        )
        if agent_root.is_symlink() or agent_root.exists():
            shutil.rmtree(agent_root)

    def cleanup_runtime_instances(self, agent_id: str) -> None:
        """删除 runtime 实例隔离目录（opencode-instances）。

        这些目录由沙箱内的 witty-agent-server 创建（XDG data/state/cache 等），
        agent 删除后同样属于泄漏，一并由 witty-service 侧回收。
        """
        validate_agent_id(agent_id)
        for name in RUNTIME_INSTANCE_DIR_NAMES:
            instance_dir = self.base_dir / name / agent_id
            self._ensure_managed_target(
                instance_dir, allowed_parent=self.base_dir / name
            )
            if instance_dir.is_symlink() or instance_dir.exists():
                shutil.rmtree(instance_dir)

    def _ensure_managed_target(self, target: Path, *, allowed_parent: Path) -> None:
        """校验删除目标确实位于本分支管理目录之内。

        两道防线：``agent_id`` 校验挡住路径穿越，解析符号链接后的包含关系挡住
        软链把删除目标指到 workspace 根之外。越界一律拒绝，不做"尽力删除"。
        """
        resolved_parent = allowed_parent.resolve()
        resolved_target = target.resolve(strict=False)
        if (
            resolved_target == resolved_parent
            or resolved_parent not in resolved_target.parents
        ):
            raise ValueError(
                f"Refusing to clean path outside managed directory: {target}"
            )

    def _agent_workspace_path(self, agent_id: str) -> Path:
        # 路径推导与校验均委托给唯一事实来源 agent_workspace_path（含 validate_agent_id）。
        return agent_workspace_path(agent_id, root=self.base_dir)


class LocalWorkspaceStore(WorkspaceStore):
    def __init__(self, base_path: str | Path = "~/.witty") -> None:
        super().__init__(base_path=base_path)
