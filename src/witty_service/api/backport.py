from __future__ import annotations

import logging
import threading
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from witty_service.api.auth import require_bearer_auth
from witty_service.api.backport_schemas import (
    BackportConfigPayload,
    BackportAsyncRunResponse,
    BackportConfigUpdateResponse,
    BackportRepositoryPrepareRequest,
    BackportRepositoryPrepareResponse,
    BackportRepositoryRefreshRequest,
    BackportRuntimeStatusResponse,
    BackportRunRequest,
    BackportRunResponse,
)
from witty_service.api.services import ServiceContainer
from witty_service.application.backport_run_store import BackportRunStore
from witty_service.application.backport_service import BackportService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/backport",
    tags=["backport"],
    dependencies=[Depends(require_bearer_auth)],
)


def get_services(request: Request) -> ServiceContainer:
    return request.app.state.services


def get_backport_service(
    services: ServiceContainer = Depends(get_services),
) -> BackportService:
    return BackportService(services)


def _ensure_backport_runs(request: Request) -> tuple[dict, threading.Lock]:
    if not hasattr(request.app.state, "backport_runs"):
        request.app.state.backport_runs = {}
        request.app.state.backport_runs_lock = threading.Lock()
    return request.app.state.backport_runs, request.app.state.backport_runs_lock


def _ensure_backport_run_store(request: Request) -> BackportRunStore:
    if not hasattr(request.app.state, "backport_run_store"):
        base_dir = request.app.state.services.workspace_store.base_dir
        request.app.state.backport_run_store = BackportRunStore(base_dir / "backport-runs")
    return request.app.state.backport_run_store


def _ensure_repository_prepare_tasks(request: Request) -> tuple[dict, threading.Lock]:
    if not hasattr(request.app.state, "backport_repository_prepare_tasks"):
        request.app.state.backport_repository_prepare_tasks = {}
        request.app.state.backport_repository_prepare_tasks_lock = threading.Lock()
    return (
        request.app.state.backport_repository_prepare_tasks,
        request.app.state.backport_repository_prepare_tasks_lock,
    )


@router.get("/config", response_model=BackportConfigPayload)
def get_config(
    backport_service: BackportService = Depends(get_backport_service),
) -> BackportConfigPayload:
    return BackportConfigPayload(**backport_service.get_config())


@router.put("/config", response_model=BackportConfigUpdateResponse)
def update_config(
    payload: BackportConfigPayload,
    backport_service: BackportService = Depends(get_backport_service),
) -> BackportConfigUpdateResponse:
    backport_service.update_config(payload.model_dump())
    return BackportConfigUpdateResponse(ok=True, config_path=backport_service.config_path)


@router.post("/runtime-status", response_model=BackportRuntimeStatusResponse)
def get_runtime_status(
    payload: BackportConfigPayload,
    backport_service: BackportService = Depends(get_backport_service),
) -> BackportRuntimeStatusResponse:
    return BackportRuntimeStatusResponse(**backport_service.get_runtime_status(payload.model_dump()))


@router.get("/browse")
def browse_path(
    path: str | None = Query(default=None),
    backport_service: BackportService = Depends(get_backport_service),
) -> dict:
    return backport_service.browse_path(path)


@router.get("/repositories/recent")
def list_recent_repositories(
    backport_service: BackportService = Depends(get_backport_service),
) -> dict:
    return backport_service.list_recent_repositories()


@router.post("/repositories/refresh")
def refresh_repository(
    payload: BackportRepositoryRefreshRequest,
    backport_service: BackportService = Depends(get_backport_service),
) -> dict:
    return backport_service.refresh_repository(
        role=payload.role,
        local_path=payload.local_path,
        source_url=payload.source_url,
        selected_branch=payload.selected_branch,
    )


@router.post("/repositories/prepare", response_model=BackportRepositoryPrepareResponse)
def prepare_repository(
    payload: BackportRepositoryPrepareRequest,
    request: Request,
) -> BackportRepositoryPrepareResponse:
    tasks, tasks_lock = _ensure_repository_prepare_tasks(request)
    task_id = uuid.uuid4().hex
    task_record = {
        "task_id": task_id,
        "status": "running",
        "role": payload.role,
        "input": payload.input,
        "progress": 0,
        "steps": [],
        "result": None,
        "error": "",
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    with tasks_lock:
        tasks[task_id] = task_record

    services = request.app.state.services

    def worker() -> None:
        def update_progress(progress: dict) -> None:
            with tasks_lock:
                task_record["progress"] = progress.get("progress", task_record["progress"])
                task_record["steps"] = progress.get("steps", task_record["steps"])
                task_record["updated_at"] = time.time()

        service = BackportService(services)
        try:
            result = service.prepare_repository(
                role=payload.role,
                raw_input=payload.input,
                preferred_branch=payload.preferred_branch,
                progress_callback=update_progress,
            )
            steps = result.pop("steps", task_record["steps"])
            with tasks_lock:
                task_record["status"] = "success"
                task_record["progress"] = 100
                task_record["steps"] = steps
                task_record["result"] = result
                task_record["updated_at"] = time.time()
        except Exception as exc:
            logger.exception("Backport repository prepare failed: task_id=%s", task_id)
            with tasks_lock:
                task_record["status"] = "failed"
                task_record["error"] = str(exc)
                task_record["progress"] = 100
                task_record["updated_at"] = time.time()

    threading.Thread(target=worker, daemon=True, name=f"backport-repo-{task_id[:8]}").start()
    return BackportRepositoryPrepareResponse(**task_record)


@router.get("/repositories/prepare/{task_id}", response_model=BackportRepositoryPrepareResponse)
def get_repository_prepare_task(
    task_id: str,
    request: Request,
) -> BackportRepositoryPrepareResponse:
    tasks, tasks_lock = _ensure_repository_prepare_tasks(request)
    with tasks_lock:
        task_record = tasks.get(task_id)
        if task_record is None:
            raise HTTPException(status_code=404, detail="Backport repository prepare task not found.")
        return BackportRepositoryPrepareResponse(**dict(task_record))


@router.post("/runs", response_model=BackportAsyncRunResponse)
def create_run(
    payload: BackportRunRequest,
    request: Request,
) -> BackportAsyncRunResponse:
    if payload.action not in {"generate_report", "run_all"}:
        raise HTTPException(status_code=400, detail="Only generate_report and run_all support async runs.")

    runs, runs_lock = _ensure_backport_runs(request)
    run_store = _ensure_backport_run_store(request)
    requested_run_id = str(
        payload.payload.get("run_id")
        or payload.payload.get("_archive_run_id")
        or ""
    ).strip()
    run_id = requested_run_id or uuid.uuid4().hex
    if run_store.safe_slug(run_id) != run_id:
        raise HTTPException(status_code=400, detail="Invalid Backport run id.")
    with runs_lock:
        current = runs.get(run_id)
        if current is not None and current.get("status") == "running":
            raise HTTPException(status_code=409, detail="Backport run is already running.")
        run_record = run_store.create_async_record(
            run_id=run_id,
            action=payload.action,
            payload=payload.payload,
        )
        run_record["created_at"] = time.time()
        run_record["updated_at"] = time.time()
        runs[run_id] = run_record

    services = request.app.state.services
    action = payload.action
    action_payload = dict(payload.payload)
    action_payload.setdefault("_archive_run_id", run_id)

    def worker() -> None:
        # 主要服务于_run_all，记录当前运行的progress
        def update_progress(progress: dict) -> None:
            with runs_lock:
                run_record["progress"] = progress
                run_record["updated_at"] = time.time()
                run_store.update_manifest(
                    run_store.runs_root / run_id,
                    {"progress": progress},
                )
                run_store.update_current_execution(
                    run_store.runs_root / run_id,
                    {"progress": progress},
                )

        def pause_requested() -> bool:
            with runs_lock:
                return bool(run_record.get("pause_requested"))

        service = BackportService(
            services,
            progress_callback=update_progress,
            pause_checker=pause_requested,
        )
        try:
            result = service.run_action(action, action_payload)
            with runs_lock:
                run_record["result"] = result
                parsed_result = result.get("parsedResult") if isinstance(result, dict) else None
                paused = isinstance(parsed_result, dict) and parsed_result.get("stage") == "paused"
                run_record["status"] = "paused" if paused else "success"
                if paused:
                    run_record["paused_at"] = time.time()
                run_record["updated_at"] = time.time()
                run_store.update_manifest(
                    run_store.runs_root / run_id,
                    {
                        "status": run_record["status"],
                        "result": result,
                        "error": "",
                        "progress": run_record.get("progress"),
                        "paused_at": run_record.get("paused_at"),
                    },
                )
                run_store.update_current_execution(
                    run_store.runs_root / run_id,
                    {
                        "status": run_record["status"],
                        "result": result,
                        "paused_at": run_record.get("paused_at"),
                    },
                )
                run_store.append_run_log(run_store.runs_root / run_id, "async action completed")
        except Exception as exc:
            logger.exception("Backport async run failed: run_id=%s action=%s", run_id, action)
            with runs_lock:
                run_record["status"] = "failed"
                run_record["error"] = str(exc)
                run_record["updated_at"] = time.time()
                run_store.update_manifest(
                    run_store.runs_root / run_id,
                    {
                        "status": "failed",
                        "result": None,
                        "error": str(exc),
                        "progress": run_record.get("progress"),
                    },
                )
                run_store.update_current_execution(
                    run_store.runs_root / run_id,
                    {"status": "failed", "error": str(exc)},
                )
                run_store.append_run_log(
                    run_store.runs_root / run_id,
                    f"async action failed error={exc}",
                )

    threading.Thread(target=worker, daemon=True, name=f"backport-{run_id[:8]}").start()
    return BackportAsyncRunResponse(**run_record)


@router.get("/runs")
def list_runs(request: Request) -> dict:
    runs, runs_lock = _ensure_backport_runs(request)
    run_store = _ensure_backport_run_store(request)
    with runs_lock:
        active_ids = {
            run_id
            for run_id, record in runs.items()
            if record.get("status") == "running"
        }
    return {"runs": run_store.list_runs(active_run_ids=active_ids)}


@router.get("/runs/{run_id}", response_model=BackportAsyncRunResponse)
def get_run(
    run_id: str,
    request: Request,
) -> BackportAsyncRunResponse:
    runs, runs_lock = _ensure_backport_runs(request)

    with runs_lock:
        run_record = runs.get(run_id)
        if run_record is not None:
            return BackportAsyncRunResponse(**dict(run_record))
    run_record = _ensure_backport_run_store(request).get_async_record(run_id, active=False)
    if run_record is None:
        raise HTTPException(status_code=404, detail="Backport run not found.")
    return BackportAsyncRunResponse(**run_record)


@router.get("/runs/{run_id}/cases/{row_key}/attempts")
def list_case_attempts(
    run_id: str,
    row_key: str,
    request: Request,
) -> dict:
    run_store = _ensure_backport_run_store(request)
    if run_store.get_async_record(run_id, active=True) is None:
        raise HTTPException(status_code=404, detail="Backport run not found.")
    return {"attempts": run_store.list_case_attempts(run_id, row_key)}


@router.get("/runs/{run_id}/executions")
def list_executions(
    run_id: str,
    request: Request,
) -> dict:
    run_store = _ensure_backport_run_store(request)
    if run_store.get_async_record(run_id, active=True) is None:
        raise HTTPException(status_code=404, detail="Backport run not found.")
    return {"executions": run_store.list_executions(run_id)}


@router.post("/runs/{run_id}/pause", response_model=BackportAsyncRunResponse)
def pause_run(
    run_id: str,
    request: Request,
) -> BackportAsyncRunResponse:
    runs, runs_lock = _ensure_backport_runs(request)
    run_store = _ensure_backport_run_store(request)
    with runs_lock:
        run_record = runs.get(run_id)
        if run_record is None:
            recovered = run_store.get_async_record(run_id, active=False)
            if recovered is None:
                raise HTTPException(status_code=404, detail="Backport run not found.")
            return BackportAsyncRunResponse(**recovered)
        if run_record.get("action") != "run_all":
            raise HTTPException(status_code=400, detail="Only run_all supports pause.")
        if run_record.get("status") != "running":
            return BackportAsyncRunResponse(**dict(run_record))
        run_record["pause_requested"] = True
        run_record["updated_at"] = time.time()
        run_store.update_manifest(
            run_store.runs_root / run_id,
            {"pause_requested": True},
        )
        return BackportAsyncRunResponse(**dict(run_record))


@router.post("/run", response_model=BackportRunResponse)
def run_action(
    payload: BackportRunRequest,
    backport_service: BackportService = Depends(get_backport_service),
) -> BackportRunResponse:
    return BackportRunResponse(**backport_service.run_action(payload.action, payload.payload))
