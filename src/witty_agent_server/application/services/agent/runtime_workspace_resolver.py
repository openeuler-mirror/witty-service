import os
from pathlib import Path

from witty_agent_server.runtimes.runtime_base import RuntimeType


WITTY_WORKSPACE_ROOT = os.environ.get("WITTY_WORKSPACE_ROOT", "~/.witty")
AGENT_CONFIG_DIR_NAME = "agent-config"
AGENT_SPEC_FILE_NAME = "agent-spec.yaml"
OPENCLAW_TEMPLATE_FILE_NAME = "openclaw-template.json"


def resolve_workspace_root(path: str = WITTY_WORKSPACE_ROOT) -> Path:
    return Path(path).expanduser()


class RuntimeWorkspaceResolver:
    def __init__(self, project_root: Path | str = WITTY_WORKSPACE_ROOT) -> None:
        self._project_root = (
            project_root
            if isinstance(project_root, Path)
            else resolve_workspace_root(project_root)
        )

    @property
    def project_root(self) -> Path:
        return self._project_root

    def get_runtime_root(self, runtime: RuntimeType) -> Path:
        if runtime == "openclaw":
            return self._project_root
        return self._project_root / runtime

    def get_agent_spec_path(self, runtime: RuntimeType) -> Path:
        return (
            self.get_runtime_root(runtime)
            / AGENT_CONFIG_DIR_NAME
            / AGENT_SPEC_FILE_NAME
        )