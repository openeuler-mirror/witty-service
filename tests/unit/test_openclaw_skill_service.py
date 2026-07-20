from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import MagicMock

import pytest

from witty_agent_server.application.services.skill.errors import (
    OpenClawSkillNotRemovableError,
    OpenClawSkillsUninstallError,
)
from witty_agent_server.application.services.skill.openclaw_skill_service import (
    OpenClawSkillService,
)


def test_install_skill_returns_runtime_file_path(monkeypatch: pytest.MonkeyPatch) -> None:
    home = Path("/runtime/test-user")
    installed_file_path = home / ".openclaw/workspace-agent-1/skills/4claw/SKILL.md"
    skill_client = MagicMock()
    skill_client.get_skills_status.return_value = {
        "skills": [
            {
                "name": "4claw",
                "eligible": True,
                "filePath": str(installed_file_path),
            }
        ]
    }
    run_mock = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=["openclaw"], returncode=0, stdout="installed", stderr=""
        )
    )
    monkeypatch.setattr(subprocess, "run", run_mock)
    monkeypatch.setattr(Path, "home", lambda: home)

    result = OpenClawSkillService(skill_client=skill_client).install_skill(
        agent_id="agent-1",
        skill_name="4claw",
    )

    assert result["filePath"] == str(installed_file_path)
    run_mock.assert_called_once_with(
        ["openclaw", "skills", "install", "4claw", "--profile", "agent-1"],
        check=True,
        capture_output=True,
        text=True,
        cwd=home,
    )
    skill_client.get_skills_status.assert_called_once_with(agent_id="agent-1")


def test_uninstall_personal_runtime_skill_removes_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    skill_dir = home / ".agents" / "skills" / "find-skills"
    skill_dir.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)

    service = OpenClawSkillService()
    result = service.uninstall_skill(
        agent_id="agent-1",
        skill_name="find-skills",
        source_type="builtin",
        source_path=str(skill_dir),
        runtime_source="agents-skills-personal",
    )

    assert result["uninstall_channel"] == "runtime_personal_remove"
    assert not skill_dir.exists()


def test_uninstall_extra_runtime_skill_rejects_symlinked_skill_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    plugin_dir = home / ".openclaw-agent-1" / "plugin-skills"
    plugin_dir.mkdir(parents=True)
    real_skill_dir = home / "extensions" / "browser-automation"
    real_skill_dir.mkdir(parents=True)
    skill_dir = plugin_dir / "browser-automation"
    skill_dir.symlink_to(real_skill_dir, target_is_directory=True)
    monkeypatch.setattr(Path, "home", lambda: home)

    service = OpenClawSkillService()
    with pytest.raises(OpenClawSkillsUninstallError):
        service.uninstall_skill(
            agent_id="agent-1",
            skill_name="browser-automation",
            source_type="builtin",
            source_path=str(skill_dir),
            runtime_source="openclaw-extra",
        )

    assert skill_dir.is_symlink()
    assert real_skill_dir.exists()


def test_validate_delete_target_rejects_skill_md_under_symlinked_skill_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    plugin_dir = home / ".openclaw-agent-1" / "plugin-skills"
    plugin_dir.mkdir(parents=True)
    real_skill_dir = home / "extensions" / "browser-automation"
    real_skill_dir.mkdir(parents=True)
    skill_dir = plugin_dir / "browser-automation"
    skill_dir.symlink_to(real_skill_dir, target_is_directory=True)
    monkeypatch.setattr(Path, "home", lambda: home)

    with pytest.raises(ValueError, match="traverses symbolic link"):
        OpenClawSkillService._validate_path_under_allowed_bases(
            skill_dir / "SKILL.md",
            agent_id="agent-1",
            runtime_source="openclaw-extra",
        )


def test_uninstall_bundled_runtime_skill_is_rejected() -> None:
    service = OpenClawSkillService()

    with pytest.raises(OpenClawSkillNotRemovableError) as exc_info:
        service.uninstall_skill(
            agent_id="agent-1",
            skill_name="healthcheck",
            source_type="builtin",
            source_path="/opt/src/node-v24.13.0-linux-arm64/lib/node_modules/openclaw/skills/healthcheck",
            runtime_source="openclaw-bundled",
        )

    assert exc_info.value.code == "OPENCLAW_SKILL_NOT_REMOVABLE"
    assert exc_info.value.details == {
        "runtime_type": "openclaw",
        "skill_name": "healthcheck",
        "reason": "bundled skill cannot be uninstalled",
    }


def test_uninstall_runtime_skill_requires_source_path() -> None:
    service = OpenClawSkillService()

    with pytest.raises(OpenClawSkillsUninstallError) as exc_info:
        service.uninstall_skill(
            agent_id="agent-1",
            skill_name="browser-automation",
            source_type="builtin",
            source_path=None,
        )

    assert exc_info.value.details == {
        "runtime_type": "openclaw",
        "skill_name": "browser-automation",
        "reason": "source_path is required for runtime-discovered skill uninstall",
    }
