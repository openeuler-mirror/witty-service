"""
共享的 pytest fixtures / 配置。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import witty_service.config as _config


@pytest.fixture(autouse=True)
def _disable_file_logging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """让 configure_logging 不写文件,避免对 ~/.witty 的权限依赖。

    日志仍通过 console handler 输出,不会"隐藏"日志。
    """
    monkeypatch.setenv("WITTY_LOG_FILE", "")
    # 隔离数据库 & workspace,避免污染 / 创建 ~/.witty
    workspace = tmp_path / "workspace"
    db_path = tmp_path / "db"
    db_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WITTY_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("WITTY_DATABASE_URL", f"sqlite:///{db_path / 'witty.sqlite3'}")
    monkeypatch.setenv("PATCHFLOW_STATE_ROOT", str(tmp_path / ".patchflow"))
    # 强制刷新 settings 缓存
    monkeypatch.setattr(_config, "_settings", None)
