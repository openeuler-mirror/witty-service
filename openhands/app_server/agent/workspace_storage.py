"""Default on-disk workspace layout for agents."""

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any


class DefaultAgentWorkspaceStorage:
    """Creates host workspace directories and optional `.agent/` state files.

    Root directory defaults to ``AGENT_WORKSPACE_ROOT`` env or ``/tmp/agent-workspaces``
    so local/tests work without a writable ``/data`` mount.
    """

    def __init__(self, root: str | None = None) -> None:
        self._root = (root or os.getenv("AGENT_WORKSPACE_ROOT", "/tmp/agent-workspaces")).rstrip("/")

    def host_workspace_path(self, agent_id: str) -> str:
        return f"{self._root}/{agent_id}"

    async def init_workspace(self, agent_id: str, workspace_path: str) -> None:
        workspace_dir = Path(workspace_path) / agent_id
        await asyncio.to_thread(workspace_dir.mkdir, parents=True, exist_ok=True)
   

    async def save_state(self, agent_id: str, state: dict[str, Any]) -> None:
        path = Path(self.host_workspace_path(agent_id)) / ".agent" / "state.json"

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, default=str, ensure_ascii=False), encoding="utf-8")

        await asyncio.to_thread(_write)

    async def cleanup(self, agent_id: str) -> None:
        path = Path(self.host_workspace_path(agent_id))
        if path.exists():
            await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
