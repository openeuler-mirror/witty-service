from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from witty_service import main as main_module


def test_create_app_closes_services_on_shutdown(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module.SkillManager,
        "sync_awesome_repository_in_background",
        lambda **_kwargs: None,
    )

    services = MagicMock()
    services.repository = MagicMock()
    services.repository.find_stale_generating_messages.return_value = []
    services.repository.list_agents_needing_recovery.return_value = []
    services.close = AsyncMock()
    services.scheduled_task_service = MagicMock()
    services.scheduled_task_service.start = AsyncMock()
    services.scheduled_task_service.shutdown = AsyncMock()

    with TestClient(main_module.create_app(services=services)):
        pass

    services.close.assert_awaited_once_with()
    services.scheduled_task_service.start.assert_awaited_once_with()
    services.scheduled_task_service.shutdown.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_scheduler_starts_after_agent_recovery(monkeypatch) -> None:
    """调度器必须在 agent 恢复完成后再启动，避免恢复窗口内触发定时任务失败。"""
    monkeypatch.setattr(
        main_module.SkillManager,
        "sync_awesome_repository_in_background",
        lambda **_kwargs: None,
    )

    services = MagicMock()
    services.repository = MagicMock()
    services.repository.find_stale_generating_messages.return_value = []
    services.repository.list_agents_needing_recovery.return_value = []
    services.close = AsyncMock()
    services.scheduled_task_service = MagicMock()
    services.scheduled_task_service.start = AsyncMock()

    app = main_module.create_app(services=services)

    startup_handlers = list(app.router.on_startup)
    names = [getattr(handler, "__name__", str(handler)) for handler in startup_handlers]
    assert names.index("recover_agents") < names.index("start_scheduled_tasks")

    calls: list[str] = []
    services.repository.list_agents_needing_recovery.side_effect = lambda **_kwargs: (
        calls.append("recovery") or []
    )
    services.scheduled_task_service.start.side_effect = lambda: calls.append(
        "scheduler"
    )

    for name in ("recover_agents", "start_scheduled_tasks"):
        result = startup_handlers[names.index(name)]()
        if inspect.isawaitable(result):
            await result

    assert calls[:2] == ["recovery", "recovery"]
    assert calls[-1] == "scheduler"
