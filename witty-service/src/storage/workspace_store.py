from __future__ import annotations

import shutil
from pathlib import Path


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
        self._validate_agent_id(agent_id)
        workspace_path = self.base_dir / agent_id / "workspace"
        resolved_workspace_path = workspace_path.resolve()
        resolved_base_dir = self.base_dir.resolve()
        if not resolved_workspace_path.is_relative_to(resolved_base_dir):
            raise ValueError(f"Workspace path escapes base_dir: {agent_id!r}")
        return workspace_path

    @staticmethod
    def _validate_agent_id(agent_id: str) -> None:
        if not agent_id:
            raise ValueError("agent_id must not be empty")
        if agent_id in {".", ".."}:
            raise ValueError(f"Invalid agent_id: {agent_id!r}")
        if any(separator in agent_id for separator in ("/", "\\")):
            raise ValueError(f"Invalid agent_id: {agent_id!r}")

        agent_path = Path(agent_id)
        if agent_path.is_absolute() or agent_path.parts != (agent_id,) or agent_path.name != agent_id:
            raise ValueError(f"Invalid agent_id: {agent_id!r}")


class LocalWorkspaceStore(WorkspaceStore):
    def __init__(self, base_path: str | Path = "~/witty-service/agent-workspaces") -> None:
        super().__init__(base_path=base_path)
