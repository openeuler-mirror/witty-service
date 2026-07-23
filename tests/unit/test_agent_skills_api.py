from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from witty_service.api import agents as agents_api
from witty_service.domain.errors import DomainError


def _skill_record(**overrides: object) -> SimpleNamespace:
    data = {
        "skill_id": "skill-1",
        "repo_id": "repo-1",
        "skill_name": "terminal-helper",
        "relative_path": "skills/terminal-helper/SKILL.md",
        "metadata": {"title": "Terminal Helper"},
        "skill_source": "git",
        "skill_md_url": "https://example.com/SKILL.md",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _repo_record(**overrides: object) -> SimpleNamespace:
    data = {"repo_id": "repo-1", "source_type": "git"}
    data.update(overrides)
    return SimpleNamespace(**data)


def _installed_record(**overrides: object) -> SimpleNamespace:
    data = {
        "agent_id": "agent-1",
        "skill_id": "skill-1",
        "source_type": "git",
        "repo_id": "repo-1",
        "skill_name": "terminal-helper",
        "installed_at": datetime(2026, 6, 3, 10, 0, tzinfo=timezone.utc),
        "relative_path": "skills/terminal-helper/SKILL.md",
        "metadata": {"title": "Terminal Helper"},
        "skill_source": "git",
        "skill_md_url": "https://example.com/SKILL.md",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _services() -> MagicMock:
    services = MagicMock()
    services.repository = MagicMock()
    services.get_agent_manager_for_agent.return_value = MagicMock()
    return services


@pytest.mark.asyncio
async def test_install_agent_skill_returns_installed_record(monkeypatch, tmp_path):
    services = _services()
    manager = MagicMock()
    installed_file_path = str(tmp_path / "workspace-agent-1/skills/terminal-helper/SKILL.md")
    manager.install_agent_skill = AsyncMock(
        return_value={"ok": True, "filePath": installed_file_path}
    )
    services.get_agent_manager_for_agent.return_value = manager

    skill_service = MagicMock()
    skill_service.get_skill_by_skill_id.return_value = _skill_record()
    skill_service.get_repository_by_repo_id.return_value = _repo_record()
    skill_service.get_skill_source_path.return_value = "/tmp/terminal-helper"
    services.repository.upsert_installed_agent_skill.return_value = _installed_record(
        relative_path=installed_file_path
    )

    monkeypatch.setattr(agents_api, "SkillManager", lambda repository: skill_service)

    resp = await agents_api.install_agent_skill(
        agent_id="agent-1",
        payload=SimpleNamespace(
            skill_id="skill-1",
            skill_name="terminal-helper",
            source_type=None,
            skill_source=None,
        ),
        services=services,
    )

    assert resp.model_dump() == {
        "agent_id": "agent-1",
        "skill_id": "skill-1",
        "source_type": "git",
        "repo_id": "repo-1",
        "skill_name": "terminal-helper",
        "installed_at": "2026-06-03T10:00:00Z",
        "relative_path": installed_file_path,
        "metadata": {"title": "Terminal Helper"},
        "skill_source": "git",
        "skill_md_url": "https://example.com/SKILL.md",
    }
    manager.install_agent_skill.assert_awaited_once_with(
        agent_id="agent-1",
        skill_name="terminal-helper",
        source_path="/tmp/terminal-helper",
    )
    assert services.repository.upsert_installed_agent_skill.call_args.kwargs["relative_path"] == (
        installed_file_path
    )


@pytest.mark.asyncio
async def test_install_agent_skill_raises_not_found_when_skill_missing(monkeypatch):
    services = _services()
    skill_service = MagicMock()
    skill_service.get_skill_by_skill_id.return_value = None
    monkeypatch.setattr(agents_api, "SkillManager", lambda repository: skill_service)

    with pytest.raises(DomainError) as exc_info:
        await agents_api.install_agent_skill(
            agent_id="agent-1",
            payload=SimpleNamespace(
                skill_id="skill-1",
                skill_name="terminal-helper",
                source_type=None,
                skill_source=None,
            ),
            services=services,
        )

    assert exc_info.value.code == "SKILL_NOT_FOUND"
    assert exc_info.value.details == {"skill_name": "terminal-helper", "skill_id": "skill-1"}


@pytest.mark.asyncio
async def test_install_agent_skill_supports_wittyhub(monkeypatch, tmp_path):
    services = _services()
    manager = MagicMock()
    installed_file_path = str(tmp_path / "workspace-agent-1/skills/vmcore-analysis/SKILL.md")
    manager.install_agent_skill = AsyncMock(
        return_value={"ok": True, "filePath": installed_file_path}
    )
    services.get_agent_manager_for_agent.return_value = manager
    wittyhub_install_id = agents_api._build_wittyhub_skill_id(
        skill_source="https://gitcode.com/openeuler/IB_Robot",
        skill_name="vmcore-analysis",
    )
    wittyhub_metadata = {
        "description": "Analyze vmcore dumps",
        "author": "openEuler",
        "category": "diagnostics",
    }
    services.repository.upsert_installed_agent_skill.return_value = _installed_record(
        skill_id=wittyhub_install_id,
        source_type="wittyhub",
        repo_id=None,
        skill_name="vmcore-analysis",
        relative_path=installed_file_path,
        metadata=wittyhub_metadata,
        skill_source="https://gitcode.com/openeuler/IB_Robot",
        skill_md_url="https://gitcode.com/openeuler/IB_Robot",
    )

    skill_manager = MagicMock()
    monkeypatch.setattr(agents_api, "SkillManager", lambda repository: skill_manager)

    resp = await agents_api.install_agent_skill(
        agent_id="agent-1",
        payload=SimpleNamespace(
            skill_id="owner/repo/vmcore-analysis",
            skill_name="vmcore-analysis",
            source_type="wittyhub",
            skill_source="https://gitcode.com/openeuler/IB_Robot",
            metadata=wittyhub_metadata,
        ),
        services=services,
    )

    assert resp.model_dump() == {
        "agent_id": "agent-1",
        "skill_id": wittyhub_install_id,
        "source_type": "wittyhub",
        "repo_id": None,
        "skill_name": "vmcore-analysis",
        "installed_at": "2026-06-03T10:00:00Z",
        "relative_path": installed_file_path,
        "metadata": wittyhub_metadata,
        "skill_source": "https://gitcode.com/openeuler/IB_Robot",
        "skill_md_url": "https://gitcode.com/openeuler/IB_Robot",
    }
    manager.install_agent_skill.assert_awaited_once_with(
        agent_id="agent-1",
        skill_name="vmcore-analysis",
        source_type="wittyhub",
        skill_source="https://gitcode.com/openeuler/IB_Robot",
    )
    services.repository.upsert_installed_agent_skill.assert_called_once_with(
        agent_id="agent-1",
        skill_id=wittyhub_install_id,
        source_type="wittyhub",
        repo_id=None,
        skill_name="vmcore-analysis",
        relative_path=installed_file_path,
        metadata=wittyhub_metadata,
        skill_source="https://gitcode.com/openeuler/IB_Robot",
        skill_md_url="https://gitcode.com/openeuler/IB_Robot",
    )
    skill_manager.get_skill_by_skill_id.assert_not_called()


def test_list_installed_agent_skills_raises_not_found_when_agent_missing():
    services = _services()
    services.repository.get_agent.return_value = None

    with pytest.raises(DomainError) as exc_info:
        agents_api.list_installed_agent_skills(agent_id="agent-1", services=services)

    assert exc_info.value.code == "AGENT_NOT_FOUND"
    assert exc_info.value.details == {"agent_id": "agent-1"}


def test_sync_installed_agent_skills_returns_records():
    services = _services()
    manager = MagicMock()
    services.get_agent_manager_for_agent.return_value = manager
    services.repository.list_installed_agent_skills.return_value = [_installed_record()]

    resp = agents_api.sync_installed_agent_skills(agent_id="agent-1", services=services)

    assert [item.model_dump() for item in resp] == [
        {
            "agent_id": "agent-1",
            "skill_id": "skill-1",
            "source_type": "git",
            "repo_id": "repo-1",
            "skill_name": "terminal-helper",
            "installed_at": "2026-06-03T10:00:00Z",
            "relative_path": "skills/terminal-helper/SKILL.md",
            "metadata": {"title": "Terminal Helper"},
            "skill_source": "git",
            "skill_md_url": "https://example.com/SKILL.md",
        }
    ]
    manager.sync_installed_agent_skills.assert_called_once_with("agent-1")


def test_sync_installed_agent_skills_raises_failed_when_runtime_sync_fails():
    services = _services()
    manager = MagicMock()
    manager.sync_installed_agent_skills.side_effect = RuntimeError("runtime down")
    services.get_agent_manager_for_agent.return_value = manager

    with pytest.raises(DomainError) as exc_info:
        agents_api.sync_installed_agent_skills(agent_id="agent-1", services=services)

    assert exc_info.value.code == "SKILL_SYNC_FAILED"
    assert exc_info.value.details == {"agent_id": "agent-1", "error": "runtime down"}


@pytest.mark.asyncio
async def test_uninstall_agent_skill_returns_removed_record(monkeypatch):
    services = _services()
    manager = MagicMock()
    manager.uninstall_agent_skill = AsyncMock(return_value=None)
    services.get_agent_manager_for_agent.return_value = manager
    services.repository.get_installed_agent_skill.return_value = _installed_record()

    skill_service = MagicMock()
    skill_service.get_skill_by_skill_id.return_value = _skill_record()
    skill_service.get_skill_source_path.return_value = "/tmp/terminal-helper"
    monkeypatch.setattr(agents_api, "SkillManager", lambda repository: skill_service)

    resp = await agents_api.uninstall_agent_skill(
        agent_id="agent-1",
        payload=SimpleNamespace(skill_id="skill-1"),
        services=services,
    )

    assert resp.model_dump()["skill_id"] == "skill-1"
    manager.uninstall_agent_skill.assert_awaited_once_with(
        agent_id="agent-1",
        skill_name="terminal-helper",
        source_type="git",
        source_path="/tmp/terminal-helper",
    )
    services.repository.delete_installed_agent_skill.assert_called_once_with(
        agent_id="agent-1",
        skill_id="skill-1",
    )


@pytest.mark.asyncio
async def test_uninstall_agent_skill_supports_wittyhub(monkeypatch):
    services = _services()
    manager = MagicMock()
    manager.uninstall_agent_skill = AsyncMock(return_value=None)
    services.get_agent_manager_for_agent.return_value = manager
    wittyhub_install_id = agents_api._build_wittyhub_skill_id(
        skill_source="https://gitcode.com/openeuler/IB_Robot",
        skill_name="vmcore-analysis",
    )
    services.repository.get_installed_agent_skill.return_value = _installed_record(
        skill_id=wittyhub_install_id,
        source_type="wittyhub",
        repo_id=None,
        skill_name="vmcore-analysis",
        relative_path="owner/repo/vmcore-analysis",
        metadata={"description": "Analyze vmcore dumps"},
        skill_source="https://gitcode.com/openeuler/IB_Robot",
        skill_md_url="https://gitcode.com/openeuler/IB_Robot",
    )

    skill_manager = MagicMock()
    monkeypatch.setattr(agents_api, "SkillManager", lambda repository: skill_manager)

    resp = await agents_api.uninstall_agent_skill(
        agent_id="agent-1",
        payload=SimpleNamespace(skill_id=wittyhub_install_id),
        services=services,
    )

    assert resp.model_dump()["source_type"] == "wittyhub"
    manager.uninstall_agent_skill.assert_awaited_once_with(
        agent_id="agent-1",
        skill_name="vmcore-analysis",
        source_type="wittyhub",
        source_path=None,
    )
    skill_manager.get_skill_by_skill_id.assert_not_called()


@pytest.mark.asyncio
async def test_uninstall_agent_skill_passes_runtime_source_for_builtin(monkeypatch, tmp_path):
    services = _services()
    manager = MagicMock()
    manager.uninstall_agent_skill = AsyncMock(return_value=None)
    services.get_agent_manager_for_agent.return_value = manager
    skill_file_path = tmp_path / "plugin-skills/browser-automation/SKILL.md"
    services.repository.get_installed_agent_skill.return_value = _installed_record(
        source_type="builtin",
        skill_name="browser-automation",
        skill_source="openclaw-extra",
        relative_path=str(skill_file_path),
    )

    skill_manager = MagicMock()
    skill_manager.get_skill_by_skill_id.return_value = None
    monkeypatch.setattr(agents_api, "SkillManager", lambda repository: skill_manager)

    await agents_api.uninstall_agent_skill(
        agent_id="agent-1",
        payload=SimpleNamespace(skill_id="skill-1"),
        services=services,
    )

    manager.uninstall_agent_skill.assert_awaited_once_with(
        agent_id="agent-1",
        skill_name="browser-automation",
        source_type="builtin",
        source_path=str(skill_file_path.parent),
        runtime_source="openclaw-extra",
    )


@pytest.mark.asyncio
async def test_uninstall_agent_skill_raises_not_found_when_record_missing(monkeypatch):
    services = _services()
    services.repository.get_installed_agent_skill.return_value = None
    monkeypatch.setattr(agents_api, "SkillManager", lambda repository: MagicMock())

    with pytest.raises(DomainError) as exc_info:
        await agents_api.uninstall_agent_skill(
            agent_id="agent-1",
            payload=SimpleNamespace(skill_id="skill-1"),
            services=services,
        )

    assert exc_info.value.code == "SKILL_NOT_FOUND"
    assert exc_info.value.details == {"agent_id": "agent-1", "skill_id": "skill-1"}
