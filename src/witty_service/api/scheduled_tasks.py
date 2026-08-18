from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, Query, Request, Response, status

from witty_service.api.auth import require_bearer_auth
from witty_service.api.scheduled_task_schemas import (
    CreateScheduledTaskRequest,
    RunScheduledTaskRequest,
    ScheduledTaskResponse,
    ScheduledTaskRunResponse,
    ScheduledTaskRunsPageResponse,
    ScheduledTaskRunWithTaskResponse,
    ScheduledTaskTemplateResponse,
    UpdateScheduledTaskRequest,
)
from witty_service.api.services import ServiceContainer
from witty_service.application.scheduled_task_service import (
    ScheduledTaskService,
    ScheduledTaskTemplate,
)
from witty_service.persistence.repositories import (
    ScheduledTaskRecord,
    ScheduledTaskRunRecord,
)

router = APIRouter(
    prefix="/scheduled-tasks",
    tags=["scheduled-tasks"],
    dependencies=[Depends(require_bearer_auth)],
)


def get_services(request: Request) -> ServiceContainer:
    return cast(ServiceContainer, request.app.state.services)


def _task_service(services: ServiceContainer) -> ScheduledTaskService:
    return services.scheduled_task_service


@router.get(
    "/templates",
    response_model=list[ScheduledTaskTemplateResponse],
)
def list_templates(
    services: ServiceContainer = Depends(get_services),
) -> list[ScheduledTaskTemplateResponse]:
    templates = _task_service(services).list_templates()
    return [_to_template_response(template) for template in templates]


@router.post(
    "",
    response_model=ScheduledTaskResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_task(
    payload: CreateScheduledTaskRequest,
    services: ServiceContainer = Depends(get_services),
) -> ScheduledTaskResponse:
    task = _task_service(services).create_task(
        name=payload.name,
        schedule_type=payload.schedule_type,
        cron_expr=payload.cron_expr,
        interval_seconds=payload.interval_seconds,
        timezone_name=payload.timezone,
        content=payload.content,
        agent_id=payload.agent_id,
        workspace_folder=payload.workspace_folder,
        enabled=payload.enabled,
    )
    return _to_task_response(task)


@router.get("", response_model=list[ScheduledTaskResponse])
def list_tasks(
    agent_id: str | None = None,
    include_runs: int | None = Query(
        default=None,
        ge=1,
        le=100,
        description="返回每个任务最近 N 条执行记录，一次请求消除 N+1 轮询",
    ),
    services: ServiceContainer = Depends(get_services),
) -> list[ScheduledTaskResponse]:
    tasks, runs_by_task = _task_service(services).list_tasks_with_runs(
        agent_id=agent_id,
        include_runs=include_runs,
    )
    return [
        _to_task_response(task, recent_runs=runs_by_task.get(task.id, []))
        for task in tasks
    ]


@router.get("/runs", response_model=ScheduledTaskRunsPageResponse)
def list_runs_page(
    limit: int = Query(default=20, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    agent_id: str | None = None,
    services: ServiceContainer = Depends(get_services),
) -> ScheduledTaskRunsPageResponse:
    """跨任务聚合分页返回全部执行记录（含任务名/agent），供执行记录页使用。"""
    items, total = _task_service(services).list_runs_page(
        limit=limit,
        offset=offset,
        agent_id=agent_id,
    )
    return ScheduledTaskRunsPageResponse(
        items=[ScheduledTaskRunWithTaskResponse.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{task_id}", response_model=ScheduledTaskResponse)
def get_task(
    task_id: str,
    services: ServiceContainer = Depends(get_services),
) -> ScheduledTaskResponse:
    task = _task_service(services).get_task(task_id)
    return _to_task_response(task)


@router.patch("/{task_id}", response_model=ScheduledTaskResponse)
def update_task(
    task_id: str,
    payload: UpdateScheduledTaskRequest,
    services: ServiceContainer = Depends(get_services),
) -> ScheduledTaskResponse:
    """部分更新：只透传客户端显式提供的字段，未提供的字段保持原值。"""
    provided = payload.model_fields_set
    kwargs: dict[str, object] = {}
    if "name" in provided:
        kwargs["name"] = payload.name
    if "schedule_type" in provided:
        kwargs["schedule_type"] = payload.schedule_type
    if "cron_expr" in provided:
        kwargs["cron_expr"] = payload.cron_expr
    if "interval_seconds" in provided:
        kwargs["interval_seconds"] = payload.interval_seconds
    if "timezone" in provided:
        kwargs["timezone_name"] = payload.timezone
    if "content" in provided:
        kwargs["content"] = payload.content
    if "workspace_folder" in provided:
        kwargs["workspace_folder"] = payload.workspace_folder
    task = _task_service(services).update_task(task_id, **kwargs)
    return _to_task_response(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(
    task_id: str,
    services: ServiceContainer = Depends(get_services),
) -> Response:
    # 缺失任务由 service 层幂等返回（204），无需在此重复查询。
    # 有在飞运行的任务会由 service 抛出 409 拒绝删除。
    _task_service(services).delete_task(task_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{task_id}/enable", response_model=ScheduledTaskResponse)
def enable_task(
    task_id: str,
    services: ServiceContainer = Depends(get_services),
) -> ScheduledTaskResponse:
    task = _task_service(services).enable_task(task_id)
    return _to_task_response(task)


@router.post("/{task_id}/disable", response_model=ScheduledTaskResponse)
def disable_task(
    task_id: str,
    services: ServiceContainer = Depends(get_services),
) -> ScheduledTaskResponse:
    task = _task_service(services).disable_task(task_id)
    return _to_task_response(task)


@router.post(
    "/{task_id}/run",
    response_model=ScheduledTaskRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_task(
    task_id: str,
    payload: RunScheduledTaskRequest | None = None,
    services: ServiceContainer = Depends(get_services),
) -> ScheduledTaskRunResponse:
    """手动触发任务；可选传入前端已建会话，缺省时后端新建会话。"""
    run = await _task_service(services).run_task_now(
        task_id,
        session_id=payload.session_id if payload else None,
    )
    return _to_run_response(run)


@router.get("/{task_id}/runs", response_model=list[ScheduledTaskRunResponse])
def list_task_runs(
    task_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    services: ServiceContainer = Depends(get_services),
) -> list[ScheduledTaskRunResponse]:
    runs = _task_service(services).list_task_runs(
        task_id=task_id,
        limit=limit,
    )
    return [_to_run_response(run) for run in runs]


@router.get(
    "/{task_id}/runs/{run_id}",
    response_model=ScheduledTaskRunResponse,
)
def get_task_run(
    task_id: str,
    run_id: str,
    services: ServiceContainer = Depends(get_services),
) -> ScheduledTaskRunResponse:
    """按 runId 精确查询单条执行记录。

    供前端在触发执行后按 runId 轮询状态/会话，避免在列表前 N 条中翻找。
    """
    run = _task_service(services).get_task_run(task_id, run_id)
    return _to_run_response(run)


def _to_task_response(
    task: ScheduledTaskRecord,
    recent_runs: list[ScheduledTaskRunRecord] | None = None,
) -> ScheduledTaskResponse:
    response = ScheduledTaskResponse.model_validate(task)
    # recent_runs 为派生字段（ORM 记录上不存在），需单独填充。
    response.recent_runs = (
        [_to_run_response(run) for run in recent_runs] if recent_runs else []
    )
    return response


def _to_run_response(run: ScheduledTaskRunRecord) -> ScheduledTaskRunResponse:
    return ScheduledTaskRunResponse.model_validate(run)


def _to_template_response(
    template: ScheduledTaskTemplate,
) -> ScheduledTaskTemplateResponse:
    return ScheduledTaskTemplateResponse(
        key=template.key,
        name=template.name,
        description=template.description,
        schedule_type=template.schedule_type,
        cron_expr=template.cron_expr,
        interval_seconds=template.interval_seconds,
        timezone=template.timezone,
        content=template.content,
        workspace_folder=template.workspace_folder,
    )
