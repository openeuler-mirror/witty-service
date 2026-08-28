from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)

from witty_service.api.auth import require_bearer_auth
from witty_service.api.backport_schemas import (
    BackportAsyncRunResponse,
    BackportCommitImportTextRequest,
    BackportConfigPayload,
    BackportConfigUpdateResponse,
    BackportRepositoryPrepareRequest,
    BackportRepositoryPrepareResponse,
    BackportRepositoryRefreshRequest,
    BackportRunRequest,
    BackportRunResponse,
    BackportRuntimeStatusResponse,
)
from witty_service.api.services import ServiceContainer
from witty_service.application.backport_commit_import import (
    MAX_COMMIT_IMPORT_BYTES,
    parse_commit_import,
)
from witty_service.application.backport_git_client import BackportGitClient
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
        request.app.state.backport_run_store = BackportRunStore(
            base_dir / "backport-runs"
        )
        # On the first Backport request after startup, converge stale running
        # records to interrupted exactly once.
        request.app.state.backport_run_store.list_runs(active_run_ids=set())
    return request.app.state.backport_run_store


def _ensure_repository_prepare_tasks(request: Request) -> tuple[dict, threading.Lock]:
    if not hasattr(request.app.state, "backport_repository_prepare_tasks"):
        request.app.state.backport_repository_prepare_tasks = {}
        request.app.state.backport_repository_prepare_tasks_lock = threading.Lock()
    return (
        request.app.state.backport_repository_prepare_tasks,
        request.app.state.backport_repository_prepare_tasks_lock,
    )


def _read_run_target(run_store: BackportRunStore, task_id: str) -> dict:
    task_dir = run_store.runs_root / task_id
    config_path = task_dir / "input" / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        config = {}
    target_path = str(config.get("target_path") or "").strip()
    if not target_path:
        raise RuntimeError("Backport Task 缺少目标仓库本地路径，不能创建或恢复 Run。")
    state = BackportGitClient.get_repo_state(target_path)
    repo_path = Path(target_path).expanduser().resolve()
    return {
        "repository": BackportGitClient.remote_url(repo_path),
        "branch": state["target_branch"],
        "head": state["target_head"],
        "clean": state["target_status_clean"],
    }


def _validate_run_target(run_store: BackportRunStore, task_id: str) -> None:
    task_dir = run_store.runs_root / task_id
    task = run_store.read_manifest(task_id)
    state = _read_run_target(run_store, task_id)
    expected = task.get("target") if isinstance(task.get("target"), dict) else {}
    expected_branch = str(expected.get("branch") or "")
    if expected_branch and state["branch"] != expected_branch:
        raise RuntimeError(
            f"目标分支不一致：预期 {expected_branch}，实际 {state['branch']}。"
        )
    if not state["clean"]:
        raise RuntimeError("目标仓库工作区不干净，请处理后再运行。")
    current_run = int(task.get("current_run") or 0)
    run = run_store.read_run(task_id, current_run) if current_run else None
    if run and run.get("status") in {"paused", "interrupted"}:
        target_end = (
            run.get("target_end") if isinstance(run.get("target_end"), dict) else {}
        )
        expected_head = str(target_end.get("head") or "")
        if expected_head and state["head"] != expected_head:
            raise RuntimeError(
                f"目标 HEAD 不一致：预期 {expected_head}，实际 {state['head']}。"
            )
        expected_remote = str(
            target_end.get("repository") or expected.get("repository") or ""
        )
        actual_remote = state["repository"]
        if expected_remote and actual_remote and actual_remote != expected_remote:
            raise RuntimeError(
                f"目标 remote 不一致：预期 {expected_remote}，实际 {actual_remote}。"
            )
    else:
        run_store.update_manifest(
            task_dir,
            {
                "target": {
                    "repository": state["repository"]
                    or str(expected.get("repository") or ""),
                    "branch": state["branch"],
                    "head": state["head"],
                }
            },
        )


@router.post("/commit-imports/preview")
async def preview_commit_import_file(file: UploadFile = File(...)) -> dict:
    """Parse a local browser CSV file without persisting its original content."""
    filename = file.filename or ""
    if not filename.lower().endswith(".csv"):
        return {
            "entries": [],
            "errors": [{"field": "file", "message": "仅支持 .csv 文件。"}],
        }
    declared_size = getattr(file, "size", None)
    if isinstance(declared_size, int) and declared_size > MAX_COMMIT_IMPORT_BYTES:
        return {
            "entries": [],
            "errors": [{"field": "file", "message": "导入内容不能超过 1 MiB。"}],
        }
    content = await file.read(MAX_COMMIT_IMPORT_BYTES + 1)
    return parse_commit_import(content, delimiter="csv").as_dict()


@router.post("/commit-imports/preview-text")
def preview_commit_import_text(payload: BackportCommitImportTextRequest) -> dict:
    return parse_commit_import(
        payload.text.encode("utf-8"), delimiter=payload.delimiter
    ).as_dict()


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
    return BackportConfigUpdateResponse(
        ok=True, config_path=backport_service.config_path
    )


@router.post("/runtime-status", response_model=BackportRuntimeStatusResponse)
def get_runtime_status(
    payload: BackportConfigPayload,
    backport_service: BackportService = Depends(get_backport_service),
) -> BackportRuntimeStatusResponse:
    return BackportRuntimeStatusResponse(
        **backport_service.get_runtime_status(payload.model_dump())
    )


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
                task_record["progress"] = progress.get(
                    "progress", task_record["progress"]
                )
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

    threading.Thread(
        target=worker, daemon=True, name=f"backport-repo-{task_id[:8]}"
    ).start()
    return BackportRepositoryPrepareResponse(**task_record)


@router.get(
    "/repositories/prepare/{task_id}", response_model=BackportRepositoryPrepareResponse
)
def get_repository_prepare_task(
    task_id: str,
    request: Request,
) -> BackportRepositoryPrepareResponse:
    tasks, tasks_lock = _ensure_repository_prepare_tasks(request)
    with tasks_lock:
        task_record = tasks.get(task_id)
        if task_record is None:
            raise HTTPException(
                status_code=404, detail="Backport repository prepare task not found."
            )
        return BackportRepositoryPrepareResponse(**dict(task_record))


@router.post("/runs", response_model=BackportAsyncRunResponse)
def create_run(
    payload: BackportRunRequest,
    request: Request,
) -> BackportAsyncRunResponse:
    if payload.action not in {"generate_report", "run_all", "prerequisite_commits"}:
        raise HTTPException(
            status_code=400,
            detail="Only generate_report, run_all and prerequisite_commits support async runs.",
        )

    if (
        payload.action in {"generate_report", "prerequisite_commits"}
        and "commit_entries" in payload.payload
    ):
        # Preview results are advisory only. Validate the confirmation payload before
        # a Task directory is allocated, then pass the normalized order downstream.
        validated_entries = BackportService(
            request.app.state.services
        ).validate_commit_entries_for_payload(payload.payload)
        payload.payload["commit_entries"] = validated_entries

    runs, runs_lock = _ensure_backport_runs(request)
    run_store = _ensure_backport_run_store(request)
    requested_run_id = str(
        payload.payload.get("run_id") or payload.payload.get("_archive_run_id") or ""
    ).strip()
    run_id = (
        uuid.uuid4().hex
        if payload.action in {"generate_report", "prerequisite_commits"}
        else requested_run_id
    )
    if not run_id:
        raise HTTPException(
            status_code=400, detail="Backport Task id is required for run_all."
        )
    if run_store.safe_slug(run_id) != run_id:
        raise HTTPException(status_code=400, detail="Invalid Backport run id.")
    with runs_lock:
        current = runs.get(run_id)
        if current is not None and current.get("status") == "running":
            if payload.action == "run_all":
                return BackportAsyncRunResponse(**dict(current))
            raise HTTPException(
                status_code=409, detail="Backport run is already running."
            )
        try:
            if payload.action == "run_all":
                _validate_run_target(run_store, run_id)
            if payload.action == "prerequisite_commits":
                # 纯 git 扫描没有 report artifact，不建 Task；记录仅存内存供轮询 getRun。
                run_record = {
                    "run_id": run_id,
                    "action": payload.action,
                    "status": "running",
                    "result": None,
                    "error": "",
                    "progress": None,
                    "pause_requested": False,
                    "paused_at": None,
                }
            else:
                run_record = run_store.create_async_record(
                    run_id=run_id,
                    action=payload.action,
                    payload=payload.payload,
                )
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # generate_report always creates a new Task and therefore chooses the id.
        run_id = run_record["run_id"]
        run_record["created_at"] = time.time()
        run_record["updated_at"] = time.time()
        runs[run_id] = run_record

    services = request.app.state.services
    action = payload.action
    is_scan = action == "prerequisite_commits"
    action_payload = dict(payload.payload)
    action_payload["_archive_run_id"] = run_id
    if action == "run_all":
        task = run_store.read_manifest(run_id)
        report_path = str(
            run_store.runs_root / run_id / str(task.get("current_report") or "")
        )
        action_payload["base_report_path"] = report_path
        config = (
            dict(action_payload.get("config"))
            if isinstance(action_payload.get("config"), dict)
            else {}
        )
        config["current_report_path"] = report_path
        action_payload["config"] = config

    def worker() -> None:
        # 主要服务于_run_all，记录当前运行的progress
        def update_progress(progress: dict) -> None:
            with runs_lock:
                execution_summary = run_store.record_progress(run_id, progress)
                if execution_summary is not None:
                    run_record["execution_summary"] = execution_summary
                run_record["progress"] = progress
                run_record["updated_at"] = time.time()

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
                parsed_result = (
                    result.get("parsedResult") if isinstance(result, dict) else None
                )
                paused = (
                    isinstance(parsed_result, dict)
                    and parsed_result.get("stage") == "paused"
                )
                operation_failed = isinstance(parsed_result, dict) and (
                    parsed_result.get("status") == "failed"
                    or parsed_result.get("stage") == "failed"
                )
                failed_count = int(
                    (run_record.get("progress") or {}).get("failed_count") or 0
                )
                archive_status = (
                    "generation_failed"
                    if operation_failed and action == "generate_report"
                    else "failed"
                    if operation_failed
                    else "paused"
                    if paused
                    else "completed_with_failures"
                    if action == "run_all" and failed_count
                    else "completed"
                    if action == "run_all"
                    else "ready"
                )
                target_end = (
                    _read_run_target(run_store, run_id) if action == "run_all" else None
                )
                run_record["status"] = (
                    "failed" if operation_failed else "paused" if paused else "success"
                )
                run_record["error"] = (
                    str(parsed_result.get("summary") or "Backport action failed.")
                    if operation_failed and isinstance(parsed_result, dict)
                    else ""
                )
                if paused:
                    run_record["paused_at"] = time.time()
                run_record["updated_at"] = time.time()
                if is_scan:
                    return
                run_store.update_manifest(
                    run_store.runs_root / run_id,
                    {
                        "status": archive_status,
                        **({"error": run_record["error"]} if operation_failed else {}),
                        **(
                            {
                                "target": {
                                    "repository": target_end["repository"],
                                    "branch": target_end["branch"],
                                    "head": target_end["head"],
                                }
                            }
                            if target_end
                            else {}
                        ),
                        "summary": {
                            "total": int(
                                (run_record.get("progress") or {}).get("total") or 0
                            ),
                            "success": max(
                                0,
                                int(
                                    (run_record.get("progress") or {}).get(
                                        "processed_count"
                                    )
                                    or 0
                                )
                                - failed_count,
                            ),
                            "failed": failed_count,
                        }
                        if action == "run_all"
                        else run_store.read_manifest(run_id).get("summary", {}),
                    },
                )
                execution_summary = run_store.update_current_execution(
                    run_store.runs_root / run_id,
                    {
                        "status": archive_status,
                        "target_end": target_end,
                        "summary": {
                            "processed": int(
                                (run_record.get("progress") or {}).get(
                                    "processed_count"
                                )
                                or 0
                            ),
                            "success": max(
                                0,
                                int(
                                    (run_record.get("progress") or {}).get(
                                        "processed_count"
                                    )
                                    or 0
                                )
                                - failed_count,
                            ),
                            "failed": failed_count,
                        },
                    },
                )
                if execution_summary is not None:
                    run_record["execution_summary"] = execution_summary
        except Exception as exc:
            logger.exception(
                "Backport async run failed: run_id=%s action=%s", run_id, action
            )
            with runs_lock:
                run_record["status"] = "failed"
                run_record["error"] = str(exc)
                run_record["updated_at"] = time.time()
                if is_scan:
                    return
                run_store.update_manifest(
                    run_store.runs_root / run_id,
                    {
                        "status": "generation_failed"
                        if action == "generate_report"
                        else "failed",
                        "error": str(exc),
                    },
                )
                execution_summary = run_store.update_current_execution(
                    run_store.runs_root / run_id,
                    {"status": "failed", "error": str(exc)},
                )
                if execution_summary is not None:
                    run_record["execution_summary"] = execution_summary

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


@router.get("/tasks")
def list_tasks(request: Request) -> dict:
    """User-facing Task history. /runs remains as a compatibility alias."""
    return list_runs(request)


@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request) -> dict:
    task = _ensure_backport_run_store(request).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Backport task not found.")
    return task


@router.get("/tasks/{task_id}/initial-report")
def get_initial_report(task_id: str, request: Request) -> Response:
    try:
        name, content = _ensure_backport_run_store(request).read_report(task_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(
            status_code=404, detail="Backport initial report not found."
        ) from None
    return Response(
        content=content,
        media_type="application/yaml",
        headers={"Content-Disposition": f'inline; filename="{name}"'},
    )


@router.get("/tasks/{task_id}/runs")
def list_task_runs(task_id: str, request: Request) -> dict:
    run_store = _ensure_backport_run_store(request)
    if run_store.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="Backport task not found.")
    return {"executions": run_store.list_executions(task_id)}


@router.get("/tasks/{task_id}/runs/{run_number}")
def get_task_run(task_id: str, run_number: int, request: Request) -> dict:
    run = _ensure_backport_run_store(request).read_run(task_id, run_number)
    if run is None:
        raise HTTPException(status_code=404, detail="Backport run not found.")
    return run


@router.get("/tasks/{task_id}/runs/{run_number}/report")
def get_task_run_report(task_id: str, run_number: int, request: Request) -> Response:
    try:
        name, content = _ensure_backport_run_store(request).read_report(
            task_id, run_number
        )
    except (FileNotFoundError, ValueError):
        raise HTTPException(
            status_code=404, detail="Backport run report not found."
        ) from None
    return Response(
        content=content,
        media_type="application/yaml",
        headers={"Content-Disposition": f'inline; filename="{name}"'},
    )


@router.get("/tasks/{task_id}/commits.csv")
def download_task_commits_csv(task_id: str, request: Request) -> Response:
    try:
        name, content = _ensure_backport_run_store(request).read_commit_csv(task_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(
            status_code=404, detail="Backport commit CSV not found."
        ) from None
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/tasks/{task_id}/cases/{case_id}")
def get_task_case(task_id: str, case_id: str, request: Request) -> dict:
    case = _ensure_backport_run_store(request).get_case(task_id, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Backport case not found.")
    return case


@router.get("/tasks/{task_id}/cases/{case_id}/artifacts/{artifact}")
def get_task_case_artifact(
    task_id: str,
    case_id: str,
    artifact: str,
    request: Request,
) -> Response:
    try:
        path, content = _ensure_backport_run_store(request).read_artifact(
            task_id, case_id, artifact
        )
    except (FileNotFoundError, ValueError):
        raise HTTPException(
            status_code=404, detail="Backport case artifact not found."
        ) from None
    media_type = "application/json" if path.suffix == ".json" else "text/plain"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{path.name}"'},
    )


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
    run_record = _ensure_backport_run_store(request).get_async_record(
        run_id, active=False
    )
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
        return BackportAsyncRunResponse(**dict(run_record))


@router.post("/run", response_model=BackportRunResponse)
def run_action(
    payload: BackportRunRequest,
    backport_service: BackportService = Depends(get_backport_service),
) -> BackportRunResponse:
    return BackportRunResponse(
        **backport_service.run_action(payload.action, payload.payload)
    )
