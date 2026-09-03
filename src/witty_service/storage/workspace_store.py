from __future__ import annotations

import shutil
from pathlib import Path

from witty_service.workspace_paths import agent_workspace_path


class WorkspaceStore:
    def __init__(
        self,
        base_dir: str | Path | None = None,
        *,
        base_path: str | Path | None = None,
    ) -> None:
        if base_dir is None and base_path is None:
            raise TypeError("WorkspaceStore requires base_dir or base_path")
        if base_dir is not None and base_path is not None and Path(base_dir) != Path(base_path):
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
        workspace_path = self._agent_workspace_path(agent_id)
        if workspace_path.exists():
            shutil.rmtree(workspace_path)

    def _agent_workspace_path(self, agent_id: str) -> Path:
        # 路径推导与校验均委托给唯一事实来源 agent_workspace_path（含 validate_agent_id）。
        return agent_workspace_path(agent_id, root=self.base_dir)


class LocalWorkspaceStore(WorkspaceStore):
    def __init__(self, base_path: str | Path = "~/.witty") -> None:
        super().__init__(base_path=base_path)
