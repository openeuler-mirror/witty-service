from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal


class RuntimeBackupStore:
    """运行时备份/恢复管理器"""

    def __init__(self, base_path: str | Path = "~/witty-service/") -> None:
        self.base_path = Path(base_path).expanduser().resolve()

    def backup(
        self,
        agent_id: str,
        runtime_type: Literal["openclaw"] = "openclaw",
    ) -> Path:
        """备份运行时文件到本地

        源: ~/.openclaw
        目标: ~/witty-service/{agent_id}/runtime_backup/.openclaw
        """
        source = Path.home() / f".{runtime_type}"
        destination = self.base_path / agent_id / "runtime_backup" / f".{runtime_type}"

        if not source.exists():
            return destination

        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        return destination

    def restore(self, agent_id: str, runtime_type: Literal["openclaw"] = "openclaw") -> Path:
        """恢复运行时备份到原位置

        源: ~/witty-service/{agent_id}/runtime_backup/.openclaw
        目标: ~/.openclaw
        """
        backup_path = self.base_path / agent_id / "runtime_backup" / f".{runtime_type}"

        if not backup_path.exists():
            raise FileNotFoundError(f"Backup not found for agent {agent_id}")

        destination = Path.home() / f".{runtime_type}"
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(backup_path, destination)
        return destination

    def backup_exists(self, agent_id: str, runtime_type: Literal["openclaw"] = "openclaw") -> bool:
        """检查备份是否存在"""
        backup_path = self.base_path / agent_id / "runtime_backup" / f".{runtime_type}"
        return backup_path.exists()

    def delete_backup(self, agent_id: str, runtime_type: Literal["openclaw"] = "openclaw") -> None:
        """删除备份"""
        backup_path = self.base_path / agent_id / "runtime_backup" / f".{runtime_type}"
        if backup_path.exists():
            shutil.rmtree(backup_path.parent)