from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from witty_service.api import scheduled_tasks as tasks_api
from witty_service.api.scheduled_task_schemas import (
    CreateScheduledTaskRequest,
    RunScheduledTaskRequest,
    UpdateScheduledTaskRequest,
)
from witty_service.api.schemas import ConversationSummaryResponse
from witty_service.application.scheduled_task_service import ScheduledTaskService
from witty_service.domain.errors import DomainError
from witty_service.persistence.repositories import (
    ScheduledTaskRecord,
    ScheduledTaskRunRecord,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _task_record(**overrides: object) -> ScheduledTaskRecord:
    data: dict[str, object] = dict(
        id="task-1",
        name="每日竞品追踪",
        schedule_type="cron",
        cron_expr="0 9 * * *",
        interval_seconds=None,
        timezone="Asia/Shanghai",
        content="追踪竞品动态",
        agent_id="agent-1",
        workspace_folder=None,
        enabled=True,
        created_at=_now(),
        updated_at=_now(),
    )
    data.update(overrides)
    return ScheduledTaskRecord(**data)  # type: ignore[arg-type]


def _run_record(**overrides: object) -> ScheduledTaskRunRecord:
    data: dict[str, object] = dict(
        id="run-1",
        task_id="task-1",
        session_id="session-1",
        status="succeeded",
        error=None,
        started_at=_now(),
        finished_at=_now(),
        created_at=_now(),
    )
    data.update(overrides)
    return ScheduledTaskRunRecord(**data)  # type: ignore[arg-type]


def _services() -> MagicMock:
    services = MagicMock()
    services.scheduled_task_service = MagicMock(spec=ScheduledTaskService)
    services.repository = MagicMock()
    return services


def test_create_task_success() -> None:
    services = _services()
    services.scheduled_task_service.create_task.return_value = _task_record()

    resp = tasks_api.create_task(
        payload=CreateScheduledTaskRequest(
            name="每日竞品追踪",
            schedule_type="cron",
            cron_expr="0 9 * * *",
            content="追踪竞品动态",
            agent_id="agent-1",
        ),
        services=services,
    )

    services.scheduled_task_service.create_task.assert_called_once_with(
        name="每日竞品追踪",
        schedule_type="cron",
        cron_expr="0 9 * * *",
        interval_seconds=None,
        timezone_name="Asia/Shanghai",
        content="追踪竞品动态",
        agent_id="agent-1",
        workspace_folder=None,
        enabled=True,
    )
    assert resp.id == "task-1"
    assert resp.name == "每日竞品追踪"


def test_create_task_schema_requires_cron_expr() -> None:
    with pytest.raises(ValidationError):
        CreateScheduledTaskRequest(
            name="t",
            schedule_type="cron",
            cron_expr=None,
            content="work",
            agent_id="agent-1",
        )


def test_create_task_schema_requires_interval_seconds() -> None:
    with pytest.raises(ValidationError):
        CreateScheduledTaskRequest(
            name="t",
            schedule_type="interval",
            interval_seconds=None,
            content="work",
            agent_id="agent-1",
        )


def test_list_tasks_filters_by_agent() -> None:
    services = _services()
    services.scheduled_task_service.list_tasks_with_runs.return_value = (
        [_task_record(), _task_record(id="task-2")],
        {},
    )

    resp = tasks_api.list_tasks(
        agent_id="agent-1", include_runs=None, services=services
    )

    services.scheduled_task_service.list_tasks_with_runs.assert_called_once_with(
        agent_id="agent-1", include_runs=None
    )
    assert [item.id for item in resp] == ["task-1", "task-2"]
    # 不传 include_runs 时 recent_runs 为空列表（向后兼容）
    assert all(item.recent_runs == [] for item in resp)


def test_list_tasks_with_include_runs_aggregates() -> None:
    services = _services()
    services.scheduled_task_service.list_tasks_with_runs.return_value = (
        [_task_record(), _task_record(id="task-2")],
        {"task-1": [_run_record()], "task-2": []},
    )

    resp = tasks_api.list_tasks(
        agent_id="agent-1",
        include_runs=10,
        services=services,
    )

    services.scheduled_task_service.list_tasks_with_runs.assert_called_once_with(
        agent_id="agent-1", include_runs=10
    )
    assert [r.id for r in resp[0].recent_runs] == ["run-1"]
    assert resp[1].recent_runs == []


def test_get_task_returns_response() -> None:
    services = _services()
    services.scheduled_task_service.get_task.return_value = _task_record()

    resp = tasks_api.get_task("task-1", services=services)

    assert resp.id == "task-1"
    assert resp.schedule_type == "cron"


def test_update_task_merges_payload() -> None:
    services = _services()
    services.scheduled_task_service.update_task.return_value = _task_record(
        name="新名称"
    )

    resp = tasks_api.update_task(
        "task-1",
        payload=UpdateScheduledTaskRequest(name="新名称"),
        services=services,
    )

    services.scheduled_task_service.update_task.assert_called_once_with(
        "task-1", name="新名称"
    )
    assert resp.name == "新名称"


def test_update_task_omitted_fields_are_not_passed() -> None:
    """未显式提供的字段不应以 None 覆盖原值（保持哨兵语义）。"""
    services = _services()
    services.scheduled_task_service.update_task.return_value = _task_record()

    tasks_api.update_task(
        "task-1",
        payload=UpdateScheduledTaskRequest(workspace_folder=None),
        services=services,
    )

    # 显式传 null 表示清空，应透传；其余省略字段不应出现
    services.scheduled_task_service.update_task.assert_called_once_with(
        "task-1", workspace_folder=None
    )


@pytest.mark.parametrize("field", ["name", "schedule_type", "timezone", "content"])
def test_update_task_rejects_explicit_null(field: str) -> None:
    """非可空字段显式传 null 应被拒绝，避免落到服务层/DB 变成 500。"""
    with pytest.raises(ValidationError):
        UpdateScheduledTaskRequest(**{field: None})


def test_update_task_allows_explicit_null_for_workspace_folder() -> None:
    """workspace_folder 显式传 null 表示清空，是唯一允许 null 的更新字段。"""
    payload = UpdateScheduledTaskRequest(workspace_folder=None)
    assert payload.model_fields_set == {"workspace_folder"}
    assert payload.workspace_folder is None


def test_update_task_allows_null_for_schedule_only_fields() -> None:
    """cron_expr/interval_seconds 的 null 由服务层按 schedule_type 语义校验，schema 不拦截。"""
    payload = UpdateScheduledTaskRequest(cron_expr=None, interval_seconds=None)
    assert payload.cron_expr is None
    assert payload.interval_seconds is None


def test_delete_task_returns_204() -> None:
    services = _services()

    resp = tasks_api.delete_task("task-1", services=services)

    assert resp.status_code == 204
    services.scheduled_task_service.delete_task.assert_called_once_with("task-1")


def test_delete_task_propagates_in_flight_409() -> None:
    services = _services()
    services.scheduled_task_service.delete_task.side_effect = DomainError(
        code="TASK_BUSY",
        message="Task has an in-flight run and cannot be deleted.",
        status_code=409,
    )

    with pytest.raises(DomainError) as exc_info:
        tasks_api.delete_task("task-1", services=services)
    assert exc_info.value.code == "TASK_BUSY"
    assert exc_info.value.status_code == 409


def test_enable_and_disable_task() -> None:
    services = _services()
    services.scheduled_task_service.enable_task.return_value = _task_record(
        enabled=True
    )
    services.scheduled_task_service.disable_task.return_value = _task_record(
        enabled=False
    )

    enabled = tasks_api.enable_task("task-1", services=services)
    disabled = tasks_api.disable_task("task-1", services=services)

    assert enabled.enabled is True
    assert disabled.enabled is False
    services.scheduled_task_service.enable_task.assert_called_once_with("task-1")
    services.scheduled_task_service.disable_task.assert_called_once_with("task-1")


@pytest.mark.asyncio
async def test_run_task_returns_accepted() -> None:
    services = _services()
    services.scheduled_task_service.run_task_now = AsyncMock(return_value=_run_record())

    resp = await tasks_api.run_task("task-1", services=services)

    services.scheduled_task_service.run_task_now.assert_awaited_once_with(
        "task-1", session_id=None
    )
    assert resp.id == "run-1"
    assert resp.status == "succeeded"
    assert resp.session_id == "session-1"


@pytest.mark.asyncio
async def test_run_task_passes_provided_session() -> None:
    services = _services()
    services.scheduled_task_service.run_task_now = AsyncMock(return_value=_run_record())

    resp = await tasks_api.run_task(
        "task-1",
        payload=RunScheduledTaskRequest(session_id="session-x"),
        services=services,
    )

    services.scheduled_task_service.run_task_now.assert_awaited_once_with(
        "task-1", session_id="session-x"
    )
    assert resp.session_id == "session-1"


def test_list_task_runs_passes_limit_through() -> None:
    # limit 的上下界由 FastAPI Query(ge=1, le=500) 在路由层校验，
    # 端点函数直接调用时原样透传。
    services = _services()
    services.scheduled_task_service.list_task_runs.return_value = [
        _run_record(),
        _run_record(id="run-2"),
    ]

    resp = tasks_api.list_task_runs(
        "task-1",
        limit=50,
        services=services,
    )

    services.scheduled_task_service.list_task_runs.assert_called_once_with(
        task_id="task-1",
        limit=50,
    )
    assert [item.id for item in resp] == ["run-1", "run-2"]


def test_get_task_run_returns_record() -> None:
    services = _services()
    services.scheduled_task_service.get_task_run.return_value = _run_record()

    resp = tasks_api.get_task_run("task-1", "run-1", services=services)

    services.scheduled_task_service.get_task_run.assert_called_once_with(
        "task-1", "run-1"
    )
    assert resp.id == "run-1"
    assert resp.task_id == "task-1"
    assert resp.session_id == "session-1"


def test_get_task_run_not_found() -> None:
    services = _services()
    services.scheduled_task_service.get_task_run.side_effect = DomainError(
        code="TASK_RUN_NOT_FOUND",
        message="Scheduled task run was not found.",
        status_code=404,
    )

    with pytest.raises(DomainError) as exc_info:
        tasks_api.get_task_run("task-1", "missing", services=services)
    assert exc_info.value.code == "TASK_RUN_NOT_FOUND"
    assert exc_info.value.status_code == 404


def test_get_task_run_rejects_run_from_other_task() -> None:
    services = _services()
    services.scheduled_task_service.get_task_run.side_effect = DomainError(
        code="TASK_RUN_NOT_FOUND",
        message="Scheduled task run was not found.",
        status_code=404,
    )

    with pytest.raises(DomainError) as exc_info:
        tasks_api.get_task_run("task-1", "run-1", services=services)
    assert exc_info.value.code == "TASK_RUN_NOT_FOUND"
    assert exc_info.value.status_code == 404


def test_list_templates_returns_builtins() -> None:
    services = _services()
    from witty_service.application.scheduled_task_service import BUILTIN_TEMPLATES

    services.scheduled_task_service.list_templates.return_value = list(
        BUILTIN_TEMPLATES
    )

    resp = tasks_api.list_templates(services=services)

    assert len(resp) == 1
    assert resp[0].key == "daily_competitor_tracking"
    assert resp[0].name == "每日竞品动态追踪"


def test_conversation_summary_carries_scheduled_task_id() -> None:
    summary = ConversationSummaryResponse(
        id="s1",
        agent_id="agent-1",
        pinned=False,
        status="idle",
        created_at=_now(),
        updated_at=_now(),
        scheduled_task_id="task-1",
    )

    assert summary.scheduled_task_id == "task-1"
