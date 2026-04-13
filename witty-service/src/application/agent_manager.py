from __future__ import annotations
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Protocol
from uuid import uuid4

from src.adapter.websocket_client_pool import AdaptorEndpoint, WebSocketClientPool
from src.adapter.websocket_protocol import OutboundMessage
from src.adapter.websocket_client import WebSocketClient
from src.domain.enums import AgentStatus, can_transition
from src.domain.errors import DomainError
from src.persistence.repositories import AgentRecord, SessionRecord
from src.sandbox.base import SandboxHandle, sandbox_not_found
from .session_manager import SessionManager

INVALID_AGENT_TRANSITION = "INVALID_AGENT_TRANSITION"
AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
SANDBOX_STATE_NOT_FOUND = "SANDBOX_STATE_NOT_FOUND"
AGENT_NOT_RUNNING = "AGENT_NOT_RUNNING"
AGENT_CREATE_FAILED = "AGENT_CREATE_FAILED"
AGENT_PAUSE_FAILED = "AGENT_PAUSE_FAILED"
AGENT_RESUME_FAILED = "AGENT_RESUME_FAILED"
AGENT_DELETE_FAILED = "AGENT_DELETE_FAILED"


@dataclass(slots=True, frozen=True)
class AgentCreateRequest:
    name: str
    sandbox_type: str
    adapter_type: str
    idle_timeout_seconds: int
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

    def create_agent(self, request: AgentCreateRequest) -> AgentCreateResult:
        agent_id = str(uuid4())
        workspace_path = str(self._workspace_store.init_workspace(agent_id))
        sandbox_handle: SandboxHandle | None = None
        try:
            self._create_agent_record(
                agent_id=agent_id,
                request=request,
                workspace_path=workspace_path,
            )
            sandbox_handle = self._sandbox_backend.start( 
                agent_id=agent_id,
                workspace_path=workspace_path,
            )
            adapter_endpoint = self._sandbox_backend.endpoint(sandbox_handle)
            self._repository.save_sandbox_state(
                agent_id,
                sandbox_payload_json=self._sandbox_handle_payload(sandbox_handle),
                adapter_base_url=adapter_endpoint.base_url,
                adapter_ready=True,
            )
            default_session = self._session_manager.create_session(agent_id)
            running_agent = self._repository.update_agent_status(
                agent_id,
                AgentStatus.running,
            )
            return AgentCreateResult(
                agent=replace(running_agent, workspace_path=workspace_path),
                default_session=default_session,
            )
        except Exception as exc:
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
        sandbox_cleaned = False
        try:
            self._sandbox_backend.cleanup(sandbox_state.handle)
            sandbox_cleaned = True
            self._repository.save_sandbox_state(
                agent_id,
                sandbox_payload_json=self._sandbox_handle_payload(sandbox_state.handle),
                adapter_base_url=sandbox_state.adapter_base_url,
                adapter_ready=False,
                last_error=None,
            )
            return self._repository.update_agent_status(agent_id, AgentStatus.paused)
        except Exception as exc:
            compensation_errors = self._compensate_sandbox_state(
                agent_id=agent_id,
                sandbox_handle=sandbox_state.handle,
                adapter_base_url=sandbox_state.adapter_base_url,
                adapter_ready=not sandbox_cleaned,
                last_error=self._error_message(exc),
                status_on_error=AgentStatus.error,
            )
            self._raise_operation_failed(
                code=AGENT_PAUSE_FAILED,
                message="Agent pause failed.",
                agent_id=agent_id,
                cause=exc,
                compensation_errors=compensation_errors,
            )

    def resume_agent(self, agent_id: str) -> AgentRecord:
        agent = self._get_agent(agent_id)
        self._ensure_transition(agent, AgentStatus.running)
        previous_sandbox_state = self._get_sandbox_state(agent_id)
        sandbox_handle: SandboxHandle | None = None
        adapter_base_url = previous_sandbox_state.adapter_base_url
        cleanup_errors: list[dict[str, str]] = []
        try:
            sandbox_handle = self._sandbox_backend.start(
                agent_id=agent_id,
                workspace_path=agent.workspace_path,
            )
            adapter_endpoint = self._sandbox_backend.endpoint(sandbox_handle)
            adapter_base_url = adapter_endpoint.base_url
            self._repository.save_sandbox_state(
                agent_id,
                sandbox_payload_json=self._sandbox_handle_payload(sandbox_handle),
                adapter_base_url=adapter_base_url,
                adapter_ready=True,
                last_error=None,
            )
            return self._repository.update_agent_status(agent_id, AgentStatus.running)
        except Exception as exc:
            if sandbox_handle is not None:
                self._collect_error(
                    cleanup_errors,
                    "sandbox_cleanup",
                    lambda: self._sandbox_backend.cleanup(sandbox_handle),
                )
            compensation_errors: list[dict[str, str]] = []
            if not cleanup_errors:
                compensation_errors = self._compensate_sandbox_state(
                    agent_id=agent_id,
                    sandbox_handle=sandbox_handle or previous_sandbox_state.handle,
                    adapter_base_url=adapter_base_url,
                    adapter_ready=False,
                    last_error=self._error_message(exc),
                    status_on_error=None,
                )
            self._raise_operation_failed(
                code=AGENT_RESUME_FAILED,
                message="Agent resume failed.",
                agent_id=agent_id,
                cause=exc,
                cleanup_errors=cleanup_errors,
                compensation_errors=compensation_errors,
            )

    async def send_message(
        self,
        agent_id: str,
        session_id: str,
        content: str,
    ) -> dict[str, Any]:
        agent = self._get_agent(agent_id)

        if agent.status is AgentStatus.paused:
            agent = self.resume_agent(agent_id)
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

        events: list[dict[str, Any]] = []
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

            events.append(event_dict)
            if event_dict["type"] == "message.completed":
                break

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
            agent = self.resume_agent(agent_id)
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

    def delete_agent(self, agent_id: str) -> None:
        agent = self._get_agent(agent_id)
        sandbox_state = self._repository.get_sandbox_state(agent_id)

        if agent.status in {AgentStatus.running, AgentStatus.paused} and sandbox_state is None:
            raise sandbox_not_found(sandbox_type=agent.sandbox_type, sandbox_id=agent_id)
        cleanup_errors: list[dict[str, str]] = []

        if sandbox_state is not None:
            self._collect_error(
                cleanup_errors,
                "sandbox_cleanup",
                lambda: self._sandbox_backend.cleanup(sandbox_state.handle),
            )

        self._collect_error(
            cleanup_errors,
            "workspace_cleanup",
            lambda: self._workspace_store.cleanup_workspace(agent_id),
        )
        self._collect_error(
            cleanup_errors,
            "agent_delete",
            lambda: self._repository.delete_agent(agent_id),
        )

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
        ws_client = self._ws_client_pool.get_client(
            agent_id=agent_id,
            endpoint=self._get_adaptor_endpoint(agent_id, session_id),
            factory=lambda url: WebSocketClient(base_url=url),
        )

        if not ws_client.is_connected:
            await ws_client.connect(session_id)

        msg: OutboundMessage = {
            "type": "message.create",
            "payload": {"message": content},
        }
        await ws_client.send(msg)
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
