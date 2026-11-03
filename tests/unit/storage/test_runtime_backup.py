from pathlib import Path
from types import SimpleNamespace

import pytest

from witty_service.config import OpenCodeSettings
from witty_service.storage import runtime_backup as runtime_backup_module
from witty_service.storage.runtime_backup import RuntimeBackupStore


def _patch_state_dir(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    """把 opencode 状态目录锚定到测试内的 home，避免触到真实 ~/.opencode。

    ``RuntimeBackupStore`` 通过 ``get_settings().opencode.state_dir`` 解析源/目标目录，
    因此这里替换模块内的 ``get_settings`` 而非 ``Path.home``。
    """
    state_dir = home / ".opencode"
    monkeypatch.setattr(
        runtime_backup_module,
        "get_settings",
        lambda: SimpleNamespace(opencode=OpenCodeSettings(state_dir=str(state_dir))),
    )
    return state_dir


class TestBackup:
    def test_backup_copies_source_to_destination(self, tmp_path: Path, monkeypatch):
        """backup copies runtime files from source to destination."""
        agent_id = "agent-1"
        store = RuntimeBackupStore(base_path=tmp_path)

        home = tmp_path / "home"
        home.mkdir(parents=True)
        state_dir = _patch_state_dir(monkeypatch, home)
        state_dir.mkdir()
        (state_dir / "config.json").write_text('{"key": "value"}', encoding="utf-8")

        dest = store.backup(agent_id)

        assert dest == tmp_path / agent_id / "runtime_backup" / ".opencode"
        assert (
            tmp_path / agent_id / "runtime_backup" / ".opencode" / "config.json"
        ).read_text(encoding="utf-8") == '{"key": "value"}'

    def test_backup_returns_destination_when_source_not_exists(
        self, tmp_path: Path, monkeypatch
    ):
        """backup returns destination path when source does not exist."""
        agent_id = "agent-1"
        store = RuntimeBackupStore(base_path=tmp_path)

        # home 存在但 .opencode 不存在
        home = tmp_path / "home"
        home.mkdir()
        _patch_state_dir(monkeypatch, home)

        dest = store.backup(agent_id)

        assert dest == tmp_path / agent_id / "runtime_backup" / ".opencode"
        assert not dest.exists()

    def test_backup_overwrites_existing_destination(self, tmp_path: Path, monkeypatch):
        """backup overwrites existing destination directory."""
        agent_id = "agent-1"
        store = RuntimeBackupStore(base_path=tmp_path)

        # Create source with new content
        home = tmp_path / "home"
        home.mkdir()
        state_dir = _patch_state_dir(monkeypatch, home)
        state_dir.mkdir()
        (state_dir / "new.txt").write_text("new content", encoding="utf-8")

        # Create existing destination with old content
        dest_dir = tmp_path / agent_id / "runtime_backup" / ".opencode"
        dest_dir.mkdir(parents=True)
        (dest_dir / "old.txt").write_text("old content", encoding="utf-8")

        store.backup(agent_id)

        assert not (dest_dir / "old.txt").exists()
        assert (dest_dir / "new.txt").read_text(encoding="utf-8") == "new content"


class TestRestore:
    def test_restore_copies_backup_to_destination(self, tmp_path: Path, monkeypatch):
        """restore copies backup from storage to home directory."""
        agent_id = "agent-1"
        store = RuntimeBackupStore(base_path=tmp_path)

        # Create backup source
        backup_path = tmp_path / agent_id / "runtime_backup" / ".opencode"
        backup_path.mkdir(parents=True)
        (backup_path / "config.json").write_text('{"restored": true}', encoding="utf-8")

        home = tmp_path / "home"
        home.mkdir()
        state_dir = _patch_state_dir(monkeypatch, home)

        dest = store.restore(agent_id)

        assert dest == state_dir
        assert (state_dir / "config.json").read_text(
            encoding="utf-8"
        ) == '{"restored": true}'

    def test_restore_raises_when_backup_not_found(self, tmp_path: Path, monkeypatch):
        """restore raises FileNotFoundError when backup does not exist."""
        agent_id = "agent-nonexistent"
        store = RuntimeBackupStore(base_path=tmp_path)

        home = tmp_path / "home"
        home.mkdir()
        _patch_state_dir(monkeypatch, home)

        with pytest.raises(
            FileNotFoundError, match=f"Backup not found for agent {agent_id}"
        ):
            store.restore(agent_id)

    def test_restore_overwrites_existing_runtime(self, tmp_path: Path, monkeypatch):
        """restore overwrites existing runtime directory at destination."""
        agent_id = "agent-1"
        store = RuntimeBackupStore(base_path=tmp_path)

        # Create backup
        backup_path = tmp_path / agent_id / "runtime_backup" / ".opencode"
        backup_path.mkdir(parents=True)
        (backup_path / "restored.txt").write_text("restored content", encoding="utf-8")

        # Create existing runtime at destination
        home = tmp_path / "home"
        home.mkdir()
        state_dir = _patch_state_dir(monkeypatch, home)
        state_dir.mkdir()
        (state_dir / "old.txt").write_text("old content", encoding="utf-8")

        store.restore(agent_id)

        assert not (state_dir / "old.txt").exists()
        assert (state_dir / "restored.txt").read_text(
            encoding="utf-8"
        ) == "restored content"


class TestBackupExists:
    def test_backup_exists_returns_true_when_backup_present(self, tmp_path: Path):
        """backup_exists returns True when backup directory exists."""
        agent_id = "agent-1"
        store = RuntimeBackupStore(base_path=tmp_path)

        # Create backup
        (tmp_path / agent_id / "runtime_backup" / ".opencode").mkdir(parents=True)

        assert store.backup_exists(agent_id) is True

    def test_backup_exists_returns_false_when_backup_missing(self, tmp_path: Path):
        """backup_exists returns False when backup directory does not exist."""
        agent_id = "agent-nonexistent"
        store = RuntimeBackupStore(base_path=tmp_path)

        assert store.backup_exists(agent_id) is False


class TestDeleteBackup:
    def test_delete_backup_removes_backup_directory(self, tmp_path: Path):
        """delete_backup removes the backup directory and its parent."""
        agent_id = "agent-1"
        store = RuntimeBackupStore(base_path=tmp_path)

        # Create backup
        backup_dir = tmp_path / agent_id / "runtime_backup" / ".opencode"
        backup_dir.mkdir(parents=True)
        (backup_dir / "file.txt").write_text("content", encoding="utf-8")

        store.delete_backup(agent_id)

        assert not backup_dir.exists()
        assert not (tmp_path / agent_id / "runtime_backup").exists()
        # Agent directory should still exist after deleting backup
        assert (tmp_path / agent_id).exists()

    def test_delete_backup_is_idempotent(self, tmp_path: Path):
        """delete_backup does not raise when backup does not exist."""
        agent_id = "agent-nonexistent"
        store = RuntimeBackupStore(base_path=tmp_path)

        # Should not raise
        store.delete_backup(agent_id)

        assert True
