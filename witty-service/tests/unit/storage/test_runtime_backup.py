from pathlib import Path

import pytest

from src.storage.runtime_backup import RuntimeBackupStore


class TestBackup:
    def test_backup_copies_source_to_destination(self, tmp_path: Path, monkeypatch):
        """backup copies runtime files from source to destination."""
        agent_id = "agent-1"
        store = RuntimeBackupStore(base_path=tmp_path)

        # Create fake source runtime directory
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        home = tmp_path / "home"
        home.mkdir(parents=True)
        (home / ".openclaw").mkdir()
        (home / ".openclaw" / "config.json").write_text('{"key": "value"}', encoding="utf-8")

        dest = store.backup(agent_id)

        assert dest == tmp_path / agent_id / "runtime_backup" / ".openclaw"
        assert (tmp_path / agent_id / "runtime_backup" / ".openclaw" / "config.json").read_text(encoding="utf-8") == '{"key": "value"}'

    def test_backup_returns_destination_when_source_not_exists(self, tmp_path: Path, monkeypatch):
        """backup returns destination path when source does not exist."""
        agent_id = "agent-1"
        store = RuntimeBackupStore(base_path=tmp_path)

        # Make home return a path where .openclaw does not exist
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()

        dest = store.backup(agent_id)

        assert dest == tmp_path / agent_id / "runtime_backup" / ".openclaw"
        assert not dest.exists()

    def test_backup_overwrites_existing_destination(self, tmp_path: Path, monkeypatch):
        """backup overwrites existing destination directory."""
        agent_id = "agent-1"
        store = RuntimeBackupStore(base_path=tmp_path)

        # Create source with new content
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: home)
        home.mkdir()
        (home / ".openclaw").mkdir()
        (home / ".openclaw" / "new.txt").write_text("new content", encoding="utf-8")

        # Create existing destination with old content
        dest_dir = tmp_path / agent_id / "runtime_backup" / ".openclaw"
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
        backup_path = tmp_path / agent_id / "runtime_backup" / ".openclaw"
        backup_path.mkdir(parents=True)
        (backup_path / "config.json").write_text('{"restored": true}', encoding="utf-8")

        # Mock home
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: home)
        home.mkdir()

        dest = store.restore(agent_id)

        assert dest == home / ".openclaw"
        assert (home / ".openclaw" / "config.json").read_text(encoding="utf-8") == '{"restored": true}'

    def test_restore_raises_when_backup_not_found(self, tmp_path: Path, monkeypatch):
        """restore raises FileNotFoundError when backup does not exist."""
        agent_id = "agent-nonexistent"
        store = RuntimeBackupStore(base_path=tmp_path)

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()

        with pytest.raises(FileNotFoundError, match=f"Backup not found for agent {agent_id}"):
            store.restore(agent_id)

    def test_restore_overwrites_existing_runtime(self, tmp_path: Path, monkeypatch):
        """restore overwrites existing runtime directory at destination."""
        agent_id = "agent-1"
        store = RuntimeBackupStore(base_path=tmp_path)

        # Create backup
        backup_path = tmp_path / agent_id / "runtime_backup" / ".openclaw"
        backup_path.mkdir(parents=True)
        (backup_path / "restored.txt").write_text("restored content", encoding="utf-8")

        # Create existing runtime at destination
        home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: home)
        home.mkdir()
        (home / ".openclaw").mkdir()
        (home / ".openclaw" / "old.txt").write_text("old content", encoding="utf-8")

        store.restore(agent_id)

        assert not (home / ".openclaw" / "old.txt").exists()
        assert (home / ".openclaw" / "restored.txt").read_text(encoding="utf-8") == "restored content"


class TestBackupExists:
    def test_backup_exists_returns_true_when_backup_present(self, tmp_path: Path):
        """backup_exists returns True when backup directory exists."""
        agent_id = "agent-1"
        store = RuntimeBackupStore(base_path=tmp_path)

        # Create backup
        (tmp_path / agent_id / "runtime_backup" / ".openclaw").mkdir(parents=True)

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
        backup_dir = tmp_path / agent_id / "runtime_backup" / ".openclaw"
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