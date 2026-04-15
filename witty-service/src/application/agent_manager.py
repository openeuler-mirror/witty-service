from __future__ import annotations

import asyncio
import json
import httpx
import logging
import time
from datetime import datetime
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Protocol
from uuid import uuid4


logger = logging.getLogger(__name__)

from sqlalchemy import false
from src.adapter.http_client import AdaptorHttpClient
from src.adapter.websocket_client_pool import AdaptorEndpoint, WebSocketClientPool
from src.adapter.websocket_protocol import OutboundMessage
from src.adapter.websocket_client import WebSocketClient
from src.domain.enums import AgentStatus, can_transition
from src.domain.errors import DomainError
from src.persistence.repositories import AgentRecord, SessionRecord
from src.sandbox.base import SandboxHandle, SandboxStatus, sandbox_not_found
from src.storage.runtime_backup import RuntimeBackupStore
from .session_manager import SessionManager

INVALID_AGENT_TRANSITION = "INVALID_AGENT_TRANSITION"
AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
SANDBOX_STATE_NOT_FOUND = "SANDBOX_STATE_NOT_FOUND"
AGENT_NOT_RUNNING = "AGENT_NOT_RUNNING"
AGENT_CREATE_FAILED = "AGENT_CREATE_FAILED"
AGENT_PAUSE_FAILED = "AGENT_PAUSE_FAILED"
AGENT_RESUME_FAILED = "AGENT_RESUME_FAILED"
AGENT_DELETE_FAILED = "AGENT_DELETE_FAILED"
RUNTIME_BACKUP_NOT_FOUND = "RUNTIME_BACKUP_NOT_FOUND"
SANDBOX_NOT_READY = "SANDBOX_NOT_READY"
RUNTIME_START_FAILED = "RUNTIME_START_FAILED"


@dataclass(slots=True, frozen=True)
class AgentCreateRequest:
    name: str
    sandbox_type: str
    adapter_type: str
    idle_timeout_seconds: int
    description: str = ""
    sandbox_id: str | None = None
    has_scheduled_tasks: bool = False


@dataclass(slots=True, frozen=True)
class AgentCreateResult:
    agent: AgentRecord
    default_session: SessionRecord


class SandboxState(Protocol):
    handle: SandboxHandle
    adapter_base_url: str


class AgentRepository(Protocol):
    def create_agent_with_id(
        self,
        *,
        agent_id: str,
        name: str,
        sandbox_type: str,
        adapter_type: str,
        workspace_path: str,
        idle_timeout_seconds: int,
        description: str = "",
        status: AgentStatus = AgentStatus.creating,
        sandbox_id: str | None = None,
        has_scheduled_tasks: bool = False,
        last_active_at: Any | None = None,
    ) -> AgentRecord: ...

    def get_agent(self, agent_id: str) -> AgentRecord | None: ...

    def update_agent_status(
        self,
        agent_id: str,
        status: AgentStatus,
        updated_at: Any | None = None,
    ) -> AgentRecord: ...

    def save_sandbox_state(
        self,
        agent_id: str,
        sandbox_payload_json: dict[str, Any],
        adapter_base_url: str,
        adapter_ready: bool = True,
        last_error: str | None = None,
    ) -> SandboxState: ...

    def get_sandbox_state(self, agent_id: str) -> SandboxState | None: ...

    def create_message(
        self,
        *,
        agent_id: str,
        session_id: str,
        role: str,
        content: str,
        metadata_json: dict[str, Any] | None = None,
    ) -> str: ...

    def delete_agent(self, agent_id: str) -> None: ...


class WorkspaceStore(Protocol):
    def init_workspace(self, agent_id: str) -> Path: ...

    def cleanup_workspace(self, agent_id: str) -> None: ...


class SandboxBackend(Protocol):
    def start(self, *, agent_id: str, workspace_path: str, **kwargs: Any) -> SandboxHandle: ...

    def stop(self, handle: SandboxHandle | str, **kwargs: Any) -> None: ...

    def endpoint(self, handle: SandboxHandle | str, **kwargs: Any) -> Any: ...

    def cleanup(self, handle: SandboxHandle | str, **kwargs: Any) -> None: ...


class AgentManager:
    def __init__(
        self,
        *,
        repository: AgentRepository,
        session_manager: SessionManager,
        workspace_store: WorkspaceStore,
        sandbox_backend: SandboxBackend,
        ws_client_pool: WebSocketClientPool | None = None,
    ) -> None:
        self._repository = repository
        self._session_manager = session_manager
        self._workspace_store = workspace_store
        self._sandbox_backend = sandbox_backend
        self._ws_client_pool = ws_client_pool or WebSocketClientPool()
        self._logger = logging.getLogger(__name__)

    def create_agent(self, request: AgentCreateRequest) -> AgentCreateResult:
        agent_id = str(uuid4())
        logger.info(f"[AgentManager] Creating agent: {request.name}, agent_id: {agent_id}")
        workspace_path = str(self._workspace_store.init_workspace(agent_id))
        logger.info(f"[AgentManager] Workspace path: {workspace_path}")
        sandbox_handle: SandboxHandle | None = None
        try:
            self._create_agent_record(
                agent_id=agent_id,
                request=request,
                workspace_path=workspace_path,
            )
            logger.info(f"[AgentManager] Agent record created, starting sandbox...")
            sandbox_handle = self._sandbox_backend.start(
                agent_id=agent_id,
                workspace_path=workspace_path,
            )
            logger.info(f"[AgentManager] Sandbox started, handle: {sandbox_handle}")
            adapter_endpoint = self._sandbox_backend.endpoint(sandbox_handle)
            logger.info(f"[AgentManager] Adapter endpoint: {adapter_endpoint.base_url}")
            sandbox_payload = self._sandbox_handle_payload(sandbox_handle)
            logger.info(f"[AgentManager] Sandbox payload: {sandbox_payload}")
            self._repository.save_sandbox_state(
                agent_id,
                sandbox_payload_json=sandbox_payload,
                adapter_base_url=adapter_endpoint.base_url,
                adapter_ready=True,
            )
            logger.info(f"[AgentManager] Sandbox state saved to database")

            # 等待适配器就绪（同步等待 /v1/ping）
            logger.info(f"[AgentManager] Waiting for sandbox to be ready...")
            client: httpx.Client | None = None
            for i in range(30):  # 30 秒超时
                try:
                    client = httpx.Client(base_url=adapter_endpoint.base_url, timeout=5.0)
                    response = client.get("/v1/ping")
                    if response.status_code == 200:
                        logger.info(f"[AgentManager] Sandbox is ready after {i+1} attempts")
                        break
                except Exception as exc:
                    logger.info(f"[AgentManager] Health check attempt {i+1} failed: {exc}")
                    pass
                finally:
                    if client is not None:
                        client.close()
                time.sleep(1)
            else:
                logger.error(f"[AgentManager] Sandbox health check timeout after 30 attempts")
                raise DomainError(
                    code=AGENT_CREATE_FAILED,
                    message="Sandbox health check timeout.",
                    details={"agent_id": agent_id},
                )

            # 调用 /agent/start 启动 witty-agent-server 中的 agent
            logger.info(f"[AgentManager] Calling /agent/start...")
            client = httpx.Client(base_url=adapter_endpoint.base_url, timeout=30.0)
            try:
                try:
                    response = client.post("/agent/start", json={})
                    logger.info(f"[AgentManager] /agent/start response: {response.status_code}")
                except httpx.HTTPStatusError as exc:
                    logger.error(f"[AgentManager] /agent/start failed: {exc}")
                    raise DomainError(
                        code=AGENT_CREATE_FAILED,
                        message="Failed to start agent.",
                        details={"agent_id": agent_id, "error": str(exc)},
                    ) from exc
            finally:
                client.close()

            # 调用 /agent/sessions 在 witty-agent-server 创建 session
            logger.info(f"[AgentManager] Calling /agent/sessions...")
            client = httpx.Client(base_url=adapter_endpoint.base_url, timeout=30.0)
            try:
                response = client.post("/agent/sessions", json={})
                logger.info(f"[AgentManager] /agent/sessions response: {response.status_code}")
                response.raise_for_status()
                session_data = response.json()
                logger.info(f"[AgentManager] Session data: {session_data}")
            except httpx.HTTPStatusError as exc:
                logger.error(f"[AgentManager] /agent/sessions failed: {exc}")
                raise DomainError(
                    code=AGENT_CREATE_FAILED,
                    message="Failed to create session on agent.",
                    details={"agent_id": agent_id, "error": str(exc)},
                ) from exc
            finally:
                client.close()

            default_session = self._session_manager.upsert_session(
                session_id=session_data["id"],
                agent_id=agent_id,
                status="active",
                context_initialized=session_data.get("context_initialized", True),
                runtime_type=session_data.get("runtime_type"),
                created_at=datetime.fromisoformat(session_data["created_at"]) if "created_at" in session_data else None,
            )
            logger.info(f"[AgentManager] Default session created: {default_session.id}")
            running_agent = self._repository.update_agent_status(
                agent_id,
                AgentStatus.running,
            )
            logger.info(f"[AgentManager] Agent status updated to running, creation complete")
            return AgentCreateResult(
                agent=replace(running_agent, workspace_path=workspace_path),
                default_session=default_session,
            )
        except Exception as exc:
            logger.error(f"[AgentManager] Agent creation failed with error: {exc}")
            logger.error(f"[AgentManager] Exception type: {type(exc).__name__}")
            logger.error(f"[AgentManager] Exception traceback:", exc_info=True)
            cleanup_errors: list[dict[str, str]] = []
            if sandbox_handle is not None:
                self._collect_error(
                    cleanup_errors,
                    "sandbox_cleanup",
                    lambda: self._sandbox_backend.cleanup(sandbox_handle),
                )
            self._collect_error(
                cleanup_errors,
                "agent_delete",
                lambda: self._repository.delete_agent(agent_id),
            )
            self._collect_error(
                cleanup_errors,
                "workspace_cleanup",
                lambda: self._workspace_store.cleanup_workspace(agent_id),
            )
            self._raise_operation_failed(
                code=AGENT_CREATE_FAILED,
                message="Agent creation failed.",
                agent_id=agent_id,
                cause=exc,
                cleanup_errors=cleanup_errors,
            )

    def pause_agent(self, agent_id: str) -> AgentRecord:
        agent = self._get_agent(agent_id)
        self._ensure_transition(agent, AgentStatus.paused)

        sandbox_state = self._get_sandbox_state(agent_id)
        client: httpx.Client | None = None
        try:
            # 调用 witty-agent-server /agent/stop 优雅停止运行时
            client = httpx.Client(base_url=sandbox_state.adapter_base_url, timeout=30.0)
            try:
                client.post("/agent/stop", json={})
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 400:  # 可能已经停止
                    raise
        finally:
            if client is not None:
                client.close()

        return self._repository.update_agent_status(agent_id, AgentStatus.paused)

    async def _resume_from_deleted(self, agent_id: str) -> AgentRecord:
        """从 deleted 状态恢复"""
        agent = self._get_agent(agent_id)

        # 1. 检查备份
        backup_store = RuntimeBackupStore()
        if not backup_store.backup_exists(agent_id, agent.adapter_type):
            raise DomainError(
                code=RUNTIME_BACKUP_NOT_FOUND,
                message="Runtime backup not found.",
                details={"agent_id": agent_id},
            )

        # 2. 恢复运行时备份
        backup_store.restore(agent_id, agent.adapter_type)

        # 3. 重新启动沙箱
        sandbox_handle = self._sandbox_backend.start(
            agent_id=agent_id,
            workspace_path=agent.workspace_path,
        )
        adapter_endpoint = self._sandbox_backend.endpoint(sandbox_handle)

        # 4. 保存沙箱状态
        self._repository.save_sandbox_state(
            agent_id,
            sandbox_payload_json=self._sandbox_handle_payload(sandbox_handle),
            adapter_base_url=adapter_endpoint.base_url,
            adapter_ready=True,
        )

        # 5. 等待沙箱就绪
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            for _ in range(30):  # 30 秒超时
                if await adaptor_client.health_check():
                    break
                await asyncio.sleep(1)
            else:
                raise DomainError(
                    code=SANDBOX_NOT_READY,
                    message="Sandbox health check timeout.",
                    details={"agent_id": agent_id},
                )
        finally:
            await adaptor_client.close()

        # 6. 调用 /agent/start
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            try:
                await adaptor_client.post("/agent/start", json={})
            except httpx.HTTPStatusError as exc:
                raise DomainError(
                    code=RUNTIME_START_FAILED,
                    message="Failed to start runtime.",
                    details={"agent_id": agent_id, "error": str(exc)},
                ) from exc
        finally:
            await adaptor_client.close()

        # 7. 更新状态
        return self._repository.update_agent_status(agent_id, AgentStatus.running)

    async def _resume_from_paused(self, agent_id: str) -> AgentRecord:
        """从 paused 状态恢复"""
        agent = self._get_agent(agent_id)

        # 1. 验证沙箱是否仍在运行
        sandbox_state = self._get_sandbox_state(agent_id)
        status = self._sandbox_backend.status(sandbox_state.handle)
        if status == SandboxStatus.stopped:
            # 沙箱已停止，降级到 deleted 场景
            return await self._resume_from_deleted(agent_id)

        # 2. 调用 /agent/start
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            try:
                await adaptor_client.post("/agent/start", json={})
            except httpx.HTTPStatusError as exc:
                raise DomainError(
                    code=RUNTIME_START_FAILED,
                    message="Failed to start runtime.",
                    details={"agent_id": agent_id, "error": str(exc)},
                ) from exc
        finally:
            await adaptor_client.close()

        # 3. 更新状态
        return self._repository.update_agent_status(agent_id, AgentStatus.running)

    async def resume_agent(self, agent_id: str) -> AgentRecord:
        agent = self._get_agent(agent_id)

        if agent.status == AgentStatus.paused:
            self._ensure_transition(agent, AgentStatus.running)
            return await self._resume_from_paused(agent_id)
        elif agent.status == AgentStatus.deleted:
            return await self._resume_from_deleted(agent_id)
        else:
            raise DomainError(
                code=INVALID_AGENT_TRANSITION,
                message="Cannot resume from current status.",
                details={"agent_id": agent_id, "status": agent.status.value},
            )

    async def send_message(
        self,
        agent_id: str,
        session_id: str,
        content: str,
        adaptor_client: AdaptorHttpClient | None = None,
    ) -> dict[str, Any]:
        self._logger.info(f"send_message called: agent_id={agent_id}, session_id={session_id}")
        agent = self._get_agent(agent_id)

        if adaptor_client is None:
            adaptor_client = self._get_adaptor_http_client(agent_id)
            client_closer = adaptor_client.close
        else:
            client_closer: Callable[[], Any] = lambda: None

        if agent.status is AgentStatus.paused:
            agent = await self.resume_agent(agent_id)
        elif agent.status is not AgentStatus.running:
            raise DomainError(
                code=AGENT_NOT_RUNNING,
                message="Agent must be running to send messages.",
                details={"agent_id": agent_id, "status": agent.status.value},
            )

        self._logger.info(f"Agent status OK: {agent.status}, preparing WebSocket client")

        self._repository.create_message(
            agent_id=agent_id,
            session_id=session_id,
            role="user",
            content=content,
        )
        ws_client = await self._prepare_ws_message_client(agent_id, session_id, content)

        self._logger.info(
            "WebSocket client ready: ws_client_id=%s is_connected=%s agent_id=%s session_id=%s",
            id(ws_client),
            ws_client.is_connected,
            agent_id,
            session_id,
        )

        events: list[dict[str, Any]] = []
        has_completed = False
        try:
            async for event in ws_client.recv():
                event_dict = dict(event)

                # Handle client.error events from witty-agent-server
                if event_dict["type"] == "client.error":
                    error_payload = event_dict.get("payload", {})
                    error_code = error_payload.get("code", "UNKNOWN_ERROR")
                    error_message = error_payload.get("message", "Unknown error from adaptor")
                    raise DomainError(
                        code=error_code,
                        message=error_message,
                        details={"session_id": session_id, "agent_id": agent_id},
                    )
                self._logger.debug(f"received event: {json.dumps(event_dict.get("payload"),indent=2,ensure_ascii=False)}")
                events.append(event_dict)
                if event_dict["type"] == "message.completed":
                    has_completed = True
                    self._logger.info("message.completed received, stopping")
                    break
        finally:
            await client_closer()

        if not has_completed:
            raise DomainError(
                code="INVALID_MESSAGE_STREAM",
                message="Message stream terminated before message.completed.",
                details={
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "events_count": len(events),
                    "last_event_type": events[-1].get("type") if events else None,
                },
            )

            (f"Returning events count: {len(events)}")
        return {
            "sandbox_type": agent.sandbox_type,
            "events": events,
        }

    async def send_message_stream(
        self,
        agent_id: str,
        session_id: str,
        content: str,
    ) -> AsyncIterator[dict[str, Any]]:
        agent = self._get_agent(agent_id)

        if agent.status is AgentStatus.paused:
            agent = await self.resume_agent(agent_id)
        elif agent.status is not AgentStatus.running:
            raise DomainError(
                code=AGENT_NOT_RUNNING,
                message="Agent must be running to send messages.",
                details={"agent_id": agent_id, "status": agent.status.value},
            )

        self._repository.create_message(
            agent_id=agent_id,
            session_id=session_id,
            role="user",
            content=content,
        )
        ws_client = await self._prepare_ws_message_client(agent_id, session_id, content)

        async for event in ws_client.recv():
            event_dict = dict(event)

            # Handle client.error events from witty-agent-server
            if event_dict["type"] == "client.error":
                error_payload = event_dict.get("payload", {})
                error_code = error_payload.get("code", "UNKNOWN_ERROR")
                error_message = error_payload.get("message", "Unknown error from adaptor")
                raise DomainError(
                    code=error_code,
                    message=error_message,
                    details={"session_id": session_id, "agent_id": agent_id},
                )

            yield {
                "sandbox_type": agent.sandbox_type,
                "event": event_dict,
            }
            if event_dict["type"] == "message.completed":
                break

    async def create_session(self, agent_id: str) -> SessionRecord:
        agent = self._get_agent(agent_id)
        if agent.status is not AgentStatus.running:
            raise DomainError(
                code=AGENT_NOT_RUNNING,
                message="Agent must be running to create session.",
                details={"agent_id": agent_id, "status": agent.status.value},
            )

        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            session = await self._session_manager.create_session_remote(agent_id, adaptor_client)
        finally:
            await adaptor_client.close()

        return session

    async def list_sessions(self, agent_id: str) -> list[SessionRecord]:
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            return await self._session_manager.list_sessions_remote(agent_id, adaptor_client)
        finally:
            await adaptor_client.close()

    async def get_session(self, agent_id: str, session_id: str) -> SessionRecord:
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            return await self._session_manager.get_session_remote(agent_id, session_id, adaptor_client)
        finally:
            await adaptor_client.close()

    async def delete_session(self, agent_id: str, session_id: str) -> None:
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            await self._session_manager.delete_session_remote(session_id, adaptor_client)
        finally:
            await adaptor_client.close()

    async def delete_agent(self, agent_id: str) -> None:
        agent = self._get_agent(agent_id)
        sandbox_state = self._repository.get_sandbox_state(agent_id)

        cleanup_errors: list[dict[str, str]] = []

        # 1. 备份运行时
        if sandbox_state is not None:
            self._collect_error(
                cleanup_errors,
                "runtime_backup",
                lambda: self._backup_runtime(agent_id, agent.adapter_type),
            )

        # 2. 停止运行时
        if agent.status in {AgentStatus.running, AgentStatus.paused}:
            try:
                await self._stop_runtime(agent_id)
            except Exception as exc:
                cleanup_errors.append({"stage": "runtime_stop", "error": self._error_message(exc)})

        # 3. 清理沙箱
        if sandbox_state is not None:
            try:
                self._cleanup_sandbox(agent_id)
            except Exception as exc:
                cleanup_errors.append({"stage": "sandbox_cleanup", "error": self._error_message(exc)})

        # 4. 保留 workspace 目录（不清除）

        # 5. 更新 agent 状态
        agent_delete_error = None
        try:
            self._repository.update_agent_status(agent_id, AgentStatus.deleted)
            logger.info(f"[AgentManager] Agent status updated to deleted in database")
        except Exception as exc:
            agent_delete_error = exc
            cleanup_errors.append({"stage": "agent_status", "error": self._error_message(exc)})
            logger.error(f"[AgentManager] Failed to update agent status: {exc}")

        # 对于删除操作，如果沙箱进程已不存在（"Sandbox handle was not found"），
        # 即使沙箱清理失败也应该允许删除成功
        if agent_delete_error is None and len(cleanup_errors) > 0:
            # 检查是否只有 sandbox_cleanup 错误且错误信息包含 "Sandbox handle was not found"
            non_sandbox_handle_errors = [
                err for err in cleanup_errors
                if not (err["stage"] == "sandbox_cleanup" and "Sandbox handle was not found" in err["error"])
            ]
            if len(non_sandbox_handle_errors) == 0:
                # 只有 "Sandbox handle was not found" 错误，允许删除成功
                logger.warning(
                    f"[AgentManager] Agent {agent_id} deleted, but sandbox was already gone: {cleanup_errors}"
                )
                return

        if cleanup_errors:
            self._raise_operation_failed(
                code=AGENT_DELETE_FAILED,
                message="Agent delete failed.",
                agent_id=agent_id,
                cause=RuntimeError(cleanup_errors[0]["error"]),
                cleanup_errors=cleanup_errors,
            )

    def _create_agent_record(
        self,
        *,
        agent_id: str,
        request: AgentCreateRequest,
        workspace_path: str,
    ) -> AgentRecord:
        return self._repository.create_agent_with_id(
            agent_id=agent_id,
            name=request.name,
            description=request.description,
            sandbox_type=request.sandbox_type,
            adapter_type=request.adapter_type,
            workspace_path=workspace_path,
            idle_timeout_seconds=request.idle_timeout_seconds,
            status=AgentStatus.creating,
            sandbox_id=request.sandbox_id,
            has_scheduled_tasks=request.has_scheduled_tasks,
        )

    def _get_agent(self, agent_id: str) -> AgentRecord:
        agent = self._repository.get_agent(agent_id)
        if agent is None:
            raise DomainError(
                code=AGENT_NOT_FOUND,
                message="Agent was not found.",
                details={"agent_id": agent_id},
            )
        return agent

    def _get_adaptor_endpoint(self, agent_id: str, session_id: str) -> AdaptorEndpoint:
        sandbox_state = self._get_sandbox_state(agent_id)
        base_url = sandbox_state.adapter_base_url
        if base_url.startswith("https"):
            scheme = "wss"
        elif base_url.startswith("http"):
            scheme = "ws"
        else:
            scheme = "ws"
        host = base_url.split("://")[-1]
        ws_base_url = f"{scheme}://{host}"
        return AdaptorEndpoint(
            base_url=ws_base_url,
            session_id=session_id,
            sandbox_type=self._get_agent(agent_id).sandbox_type,
        )

    async def _prepare_ws_message_client(
        self,
        agent_id: str,
        session_id: str,
        content: str,
    ) -> WebSocketClient:
        endpoint = self._get_adaptor_endpoint(agent_id, session_id)
        self._logger.info(f"_prepare_ws: agent_id={agent_id}, session_id={session_id}, endpoint={endpoint}")
        ws_client = self._ws_client_pool.get_client(
            agent_id=agent_id,
            endpoint=endpoint,
            factory=lambda url: WebSocketClient(base_url=url),
        )

        self._logger.info(
            "_prepare_ws: pool returned client: ws_client_id=%s is_connected=%s agent_id=%s session_id=%s",
            id(ws_client),
            ws_client.is_connected,
            agent_id,
            session_id,
        )

        if not ws_client.is_connected:
            self._logger.info(f"_prepare_ws: connecting to {endpoint.base_url}/agent/sessions/{session_id}/ws")
            await ws_client.connect(session_id)
            self._logger.info(f"_prepare_ws: connected successfully")

        msg: OutboundMessage = {
            "type": "message.create",
            "payload": {"message": content},
        }
        self._logger.info(f"_prepare_ws: sending message: {msg}")
        await ws_client.send(msg)
        self._logger.info(f"_prepare_ws: message sent")
        return ws_client

    def _get_sandbox_state(self, agent_id: str) -> SandboxState:
        sandbox_state = self._repository.get_sandbox_state(agent_id)
        if sandbox_state is None:
            raise DomainError(
                code=SANDBOX_STATE_NOT_FOUND,
                message="Sandbox state was not found.",
                details={"agent_id": agent_id},
            )
        return sandbox_state

    def _get_adaptor_http_client(self, agent_id: str) -> AdaptorHttpClient:
        """获取到 witty-agent-server 的 HTTP 客户端"""
        sandbox_state = self._get_sandbox_state(agent_id)
        return AdaptorHttpClient(base_url=sandbox_state.adapter_base_url)

    async def _check_sandbox_health(self, agent_id: str) -> bool:
        """检查沙箱是否健康存活"""
        try:
            adaptor_client = self._get_adaptor_http_client(agent_id)
            try:
                return await adaptor_client.health_check()
            finally:
                await adaptor_client.close()
        except Exception:
            return False

    def _check_and_update_agent_status_if_needed(self, agent_id: str) -> AgentRecord:
        """检查沙箱健康状态，如果进程停止则更新 agent 状态为 error"""
        agent = self._get_agent(agent_id)

        if agent.status not in {AgentStatus.running, AgentStatus.paused}:
            return agent

        # 只对 local_process 类型进行检查
        if agent.sandbox_type != "local_process":
            return agent

        # 检查沙箱进程是否还在运行
        sandbox_state = self._repository.get_sandbox_state(agent_id)
        if sandbox_state is None:
            return agent

        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        is_healthy = loop.run_until_complete(self._check_sandbox_health(agent_id))

        if not is_healthy and agent.status == AgentStatus.running:
            # 沙箱进程已停止，更新状态为 error
            return self._repository.update_agent_status(agent_id, AgentStatus.error)

        return agent

    def _backup_runtime(self, agent_id: str, runtime_type: str = "openclaw") -> Path | None:
        """备份运行时文件"""
        backup_store = RuntimeBackupStore()
        try:
            return backup_store.backup(agent_id, runtime_type)
        except Exception:
            return None

    def _cleanup_sandbox(self, agent_id: str) -> None:
        """清理沙箱"""
        sandbox_state = self._repository.get_sandbox_state(agent_id)
        if sandbox_state is not None:
            self._sandbox_backend.cleanup(sandbox_state.handle)

    async def _stop_runtime(self, agent_id: str) -> None:
        """停止 witty-agent-server 运行时"""
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            try:
                await adaptor_client.post("/agent/stop", json={})
            except httpx.HTTPStatusError:
                pass  # 可能已经停止
        finally:
            await adaptor_client.close()

    def _compensate_sandbox_state(
        self,
        *,
        agent_id: str,
        sandbox_handle: SandboxHandle,
        adapter_base_url: str,
        adapter_ready: bool,
        last_error: str,
        status_on_error: AgentStatus | None,
    ) -> list[dict[str, str]]:
        compensation_errors: list[dict[str, str]] = []
        self._collect_error(
            compensation_errors,
            "sandbox_state_rollback",
            lambda: self._repository.save_sandbox_state(
                agent_id,
                sandbox_payload_json=self._sandbox_handle_payload(sandbox_handle),
                adapter_base_url=adapter_base_url,
                adapter_ready=adapter_ready,
                last_error=last_error,
            ),
        )
        if status_on_error is not None:
            compensation_errors.extend(
                self._compensate_status_only(
                    agent_id=agent_id,
                    status=status_on_error,
                )
            )
        return compensation_errors

    def _compensate_status_only(
        self,
        *,
        agent_id: str,
        status: AgentStatus,
    ) -> list[dict[str, str]]:
        compensation_errors: list[dict[str, str]] = []
        self._collect_error(
            compensation_errors,
            "agent_status_error",
            lambda: self._repository.update_agent_status(agent_id, status),
        )
        return compensation_errors

    @staticmethod
    def _collect_error(
        errors: list[dict[str, str]],
        stage: str,
        action: Callable[[], Any],
    ) -> None:
        try:
            action()
        except Exception as exc:
            errors.append({"stage": stage, "error": AgentManager._error_message(exc)})

    @staticmethod
    def _error_message(exc: Exception) -> str:
        return exc.message if isinstance(exc, DomainError) else str(exc)

    def _raise_operation_failed(
        self,
        *,
        code: str,
        message: str,
        agent_id: str,
        cause: Exception,
        cleanup_errors: list[dict[str, str]] | None = None,
        compensation_errors: list[dict[str, str]] | None = None,
    ) -> None:
        details: dict[str, Any] = {
            "agent_id": agent_id,
            "cause": self._error_message(cause),
            "cleanup_errors": list(cleanup_errors or []),
        }
        if isinstance(cause, DomainError):
            details["cause_code"] = cause.code
        if compensation_errors:
            details["compensation_errors"] = list(compensation_errors)
        raise DomainError(code=code, message=message, details=details) from cause

    @staticmethod
    def _ensure_transition(agent: AgentRecord, target: AgentStatus) -> None:
        if can_transition(agent.status, target):
            return
        raise DomainError(
            code=INVALID_AGENT_TRANSITION,
            message="Agent status transition is not allowed.",
            details={
                "agent_id": agent.id,
                "from_status": agent.status.value,
                "to_status": target.value,
            },
        )

    @staticmethod
    def _sandbox_handle_payload(handle: SandboxHandle) -> dict[str, Any]:
        return {
            "sandbox_id": handle.sandbox_id,
            "agent_id": handle.agent_id,
            "workspace_path": handle.workspace_path,
            "metadata": dict(handle.metadata),
        }
