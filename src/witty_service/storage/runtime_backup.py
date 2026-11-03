from __future__ import annotations

import shutil
from pathlib import Path

from witty_service.config import get_settings


class RuntimeBackupStore:
    """运行时备份/恢复管理器"""

    def __init__(self, base_path: str | Path = "~/.witty") -> None:
        self.base_path = Path(base_path).expanduser().resolve()

    def _runtime_state_dir(self, runtime_type: str) -> Path:
        """按 runtime_type 解析状态目录，优先使用配置。"""
        settings = get_settings()
        if runtime_type == "opencode":
            return settings.opencode.state_dir_resolved()
        return Path.home() / f".{runtime_type}"

    def backup(
        self,
        agent_id: str,
        runtime_type: str = "opencode",
    ) -> Path:
        """备份运行时文件到本地

        源: <runtime state_dir>
        目标: ~/.witty/{agent_id}/runtime_backup/.{runtime_type}
        """
        source = self._runtime_state_dir(runtime_type)
        destination = self.base_path / agent_id / "runtime_backup" / f".{runtime_type}"

        if not source.exists():
            return destination

        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        return destination

    def restore(self, agent_id: str, runtime_type: str = "opencode") -> Path:
        """恢复运行时备份到原位置

        源: ~/.witty/{agent_id}/runtime_backup/.{runtime_type}
        目标: <runtime state_dir>
        """
        backup_path = self.base_path / agent_id / "runtime_backup" / f".{runtime_type}"

        if not backup_path.exists():
            raise FileNotFoundError(f"Backup not found for agent {agent_id}")

        destination = self._runtime_state_dir(runtime_type)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(backup_path, destination)
        return destination

    def backup_exists(self, agent_id: str, runtime_type: str = "opencode") -> bool:
        """检查备份是否存在"""
        backup_path = self.base_path / agent_id / "runtime_backup" / f".{runtime_type}"
        return backup_path.exists()

    def delete_backup(self, agent_id: str, runtime_type: str = "opencode") -> None:
        """删除备份"""
        backup_path = self.base_path / agent_id / "runtime_backup" / f".{runtime_type}"
        if backup_path.exists():
            shutil.rmtree(backup_path.parent)
