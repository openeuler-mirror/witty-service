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
    BackportRunRequest,
    BackportRunResponse,
)
from witty_service.api.services import ServiceContainer
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


@router.get("/browse")
def browse_path(
    path: str | None = Query(default=None),
    backport_service: BackportService = Depends(get_backport_service),
) -> dict:
    return backport_service.browse_path(path)


@router.post("/runs", response_model=BackportAsyncRunResponse)
def create_run(
    payload: BackportRunRequest,
    request: Request,
) -> BackportAsyncRunResponse:
    if payload.action not in {"generate_report", "run_all"}:
        raise HTTPException(status_code=400, detail="Only generate_report and run_all support async runs.")

    if not hasattr(request.app.state, "backport_runs"):
        request.app.state.backport_runs = {}
        request.app.state.backport_runs_lock = threading.Lock()

    runs = request.app.state.backport_runs
    runs_lock = request.app.state.backport_runs_lock
    run_id = uuid.uuid4().hex
    run_record = {
        "run_id": run_id,
        "action": payload.action,
        "status": "running",
        "result": None,
        "error": "",
        "progress": None,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    with runs_lock:
        runs[run_id] = run_record

    services = request.app.state.services
    action = payload.action
    action_payload = payload.payload

    def worker() -> None:
        # 主要服务于_run_all，记录当前运行的progress
        def update_progress(progress: dict) -> None:
            with runs_lock:
                run_record["progress"] = progress
                run_record["updated_at"] = time.time()

        service = BackportService(services, progress_callback=update_progress)
        try:
            result = service.run_action(action, action_payload)
            with runs_lock:
                run_record["status"] = "success"
                run_record["result"] = result
                run_record["updated_at"] = time.time()
        except Exception as exc:
            logger.exception("Backport async run failed: run_id=%s action=%s", run_id, action)
            with runs_lock:
                run_record["status"] = "failed"
                run_record["error"] = str(exc)
                run_record["updated_at"] = time.time()

    threading.Thread(target=worker, daemon=True, name=f"backport-{run_id[:8]}").start()
    return BackportAsyncRunResponse(**run_record)


@router.get("/runs/{run_id}", response_model=BackportAsyncRunResponse)
def get_run(
    run_id: str,
    request: Request,
) -> BackportAsyncRunResponse:
    if not hasattr(request.app.state, "backport_runs"):
        request.app.state.backport_runs = {}
        request.app.state.backport_runs_lock = threading.Lock()

    with request.app.state.backport_runs_lock:
        run_record = request.app.state.backport_runs.get(run_id)
        if run_record is None:
            raise HTTPException(status_code=404, detail="Backport run not found.")
        return BackportAsyncRunResponse(**dict(run_record))


@router.post("/run", response_model=BackportRunResponse)
def run_action(
    payload: BackportRunRequest,
    backport_service: BackportService = Depends(get_backport_service),
) -> BackportRunResponse:
    return BackportRunResponse(**backport_service.run_action(payload.action, payload.payload))
