from __future__ import annotations

import asyncio
import json
import httpx
import logging
import time
from datetime import datetime
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Protocol
from uuid import NAMESPACE_URL, uuid4, uuid5


logger = logging.getLogger(__name__)

_recovery_lock = asyncio.Lock()


def _log_prefix(agent_id: str | None = None, session_id: str | None = None) -> str:
    parts = []
    if agent_id:
        parts.append(f"agent_id={agent_id}")
    if session_id:
        parts.append(f"session_id={session_id}")
    if parts:
        return f"[{', '.join(parts)}] "
    return ""

from sqlalchemy import false
from witty_service.adapter.http_client import AdaptorHttpClient
from witty_service.adapter.websocket_client_pool import AdaptorEndpoint, WebSocketClientPool
from witty_service.adapter.websocket_protocol import OutboundMessage
from witty_service.adapter.websocket_client import WebSocketClient
from witty_service.domain.enums import AgentStatus, can_transition
from witty_service.domain.errors import DomainError
from witty_service.persistence.orm import MessageStatus
from witty_service.persistence.repositories import AgentRecord, SessionRecord
from witty_service.sandbox.base import SandboxHandle, SandboxStatus, sandbox_not_found, SANDBOX_NOT_FOUND
from witty_service.storage.runtime_backup import RuntimeBackupStore
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
SKILL_NOT_FOUND = "SKILL_NOT_FOUND"
SKILL_INSTALL_RECORD_FAILED = "SKILL_INSTALL_RECORD_FAILED"
SKILL_UNINSTALL_RECORD_FAILED = "SKILL_UNINSTALL_RECORD_FAILED"
SKILL_SYNC_FAILED = "SKILL_SYNC_FAILED"
AGENT_SKILL_INSTALL_FAILED = "AGENT_SKILL_INSTALL_FAILED"
AGENT_SKILL_UNINSTALL_FAILED = "AGENT_SKILL_UNINSTALL_FAILED"

INTERRUPTION_PREFIX = """[CRITICAL SYSTEM INSTRUCTION - OVERRIDE ALL PREVIOUS CONTEXT]

The assistant's previous response in the conversation history was INTERRUPTED and INCOMPLETE before being sent to you.

You MUST follow these rules with HIGHEST PRIORITY:

1. IGNORE the ENTIRE interrupted assistant message completely - treat it as if it never existed
2. DO NOT continue, complete, reference, or acknowledge that interrupted response in ANY way
3. DO NOT use phrases like "continuing from", "as I was saying", "to complete my previous thought"
4. Answer ONLY and DIRECTLY the user's message below, starting from a fresh response

The user's current message (ignore everything before this):

"""


@dataclass(slots=True, frozen=True)
class AgentCreateRequest:
    name: str
    sandbox_type: str
    adapter_type: str
    idle_timeout_seconds: int
    description: str = ""
    sandbox_id: str | None = None
    has_scheduled_tasks: bool = False
    model_id: str | None = None
    mcp_server_list: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class AgentCreateResult:
    agent: AgentRecord


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
        model_id: str | None = None,
        mcp_server_list: list[str] = [],
        last_active_at: Any | None = None,
    ) -> AgentRecord: ...

    def get_agent(self, agent_id: str) -> AgentRecord | None: ...

    def list_agents_needing_recovery(
        self, sandbox_type: str | None = None, status_filter: list[AgentStatus] | None = None
    ) -> list[AgentRecord]: ...

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

    def create_message_event_with_retry(
        self,
        *,
        agent_id: str,
        session_id: str,
        event_type: str,
        payload_json: dict[str, Any],
        seq_no: int,
        message_id: str | None = None,
        max_retries: int = 5,
    ) -> tuple[str, int]: ...

    def create_assistant_message_and_bind_events(
        self,
        *,
        agent_id: str,
        session_id: str,
        content: str,
        event_ids: list[str],
        metadata_json: dict[str, Any] | None = None,
    ) -> str: ...

    def get_last_assistant_status(self, session_id: str) -> str | None: ...

    def get_first_user_message(self, session_id: str) -> str | None: ...

    def update_session_metadata(
        self,
        session_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
    ) -> Any: ...

    def update_message_content(self, message_id: str, content: str) -> None: ...

    def update_message_stream_at(self, message_id: str) -> None: ...

    def update_message_status(self, message_id: str, status: Any) -> None: ...

    def find_stale_generating_messages(
        self, stale_threshold_seconds: int
    ) -> list[Any]: ...

    def find_last_assistant_message_for_session(self, session_id: str) -> Any | None: ...

    def update_session_runtime_identity(
        self,
        *,
        session_id: str,
        runtime_type: str,
        runtime_session_id: str,
        runtime_session_key: str,
    ) -> SessionRecord: ...

    def compact_message_delta_events(self, message_id: str) -> None: ...

    def delete_agent(self, agent_id: str) -> None: ...

    def upsert_builtin_skill(
        self,
        *,
        skill_id: str,
        skill_name: str,
        metadata: dict[str, Any],
        skill_source: str | None = None,
        relative_path: str | None = None,
    ) -> Any: ...

    def upsert_installed_agent_skill(
        self,
        *,
        agent_id: str,
        skill_id: str,
        source_type: str,
        skill_name: str,
        repo_id: str | None = None,
        relative_path: str | None = None,
        metadata: dict[str, Any] | None = None,
        skill_source: str | None = None,
        skill_md_url: str | None = None,
        installed_at: datetime | None = None,
    ) -> Any: ...

    def replace_installed_agent_skills_from_runtime(
        self,
        *,
        agent_id: str,
        skills: list[dict[str, Any]],
    ) -> None: ...

    def get_model(self, model_id: str) -> Any | None: ...


class WorkspaceStore(Protocol):
    def init_workspace(self, agent_id: str) -> Path: ...

    def cleanup_workspace(self, agent_id: str) -> None: ...


class SandboxBackend(Protocol):
    def start(self, *, agent_id: str, workspace_path: str, **kwargs: Any) -> SandboxHandle: ...

    def stop(self, handle: SandboxHandle | str, **kwargs: Any) -> None: ...

    def endpoint(self, handle: SandboxHandle | str, **kwargs: Any) -> Any: ...

    def cleanup(self, handle: SandboxHandle | str, **kwargs: Any) -> None: ...


class SessionStreamRegistry:
    """Broadcast stream events to multiple SSE subscribers per session.

    When a client disconnects (page refresh), the background WS consumer
    keeps running so the generation completes and events are persisted.
    Reconnecting clients get buffered events first, then live events.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[dict | None]]] = {}
        self._buffers: dict[str, list[dict[str, Any]]] = {}
        self._terminated: set[str] = set()
        self._generation: dict[str, int] = {}

    def start_stream(self, session_id: str) -> int:
        self.end_stream(session_id)
        gen = self._generation.get(session_id, 0) + 1
        self._generation[session_id] = gen
        self._subscribers[session_id] = []
        self._buffers[session_id] = []
        self._terminated.discard(session_id)
        return gen

    def push_event(self, session_id: str, event: dict[str, Any], generation: int) -> None:
        if self._generation.get(session_id) != generation:
            return  # Stale generation — ignore
        if session_id in self._buffers:
            self._buffers[session_id].append(event)
        for q in self._subscribers.get(session_id, []):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def end_stream(self, session_id: str) -> None:
        self._terminated.add(session_id)
        for q in self._subscribers.get(session_id, []):
            try:
                q.put_nowait(None)  # sentinel
            except asyncio.QueueFull:
                pass

    def is_active(self, session_id: str) -> bool:
        return session_id in self._subscribers and session_id not in self._terminated

    def get_buffered_events(self, session_id: str) -> list[dict[str, Any]]:
        return list(self._buffers.get(session_id, []))

    def subscribe(self, session_id: str) -> asyncio.Queue[dict | None]:
        q: asyncio.Queue[dict | None] = asyncio.Queue()
        self._subscribers.setdefault(session_id, []).append(q)
        return q

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[dict | None]) -> None:
        subs = self._subscribers.get(session_id, [])
        if queue in subs:
            subs.remove(queue)

    def has_subscribers(self, session_id: str) -> bool:
        return bool(self._subscribers.get(session_id, []))

    def cleanup(self, session_id: str) -> None:
        self._subscribers.pop(session_id, None)
        self._buffers.pop(session_id, None)
        self._terminated.discard(session_id)
        self._generation.pop(session_id, None)


_stream_registry = SessionStreamRegistry()


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

    def _find_free_port(self) -> int:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _is_port_in_use(self, port: int) -> bool:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return False
            except OSError:
                return True

    def _get_available_gateway_port(self, preferred_port: int | None) -> int:
        if preferred_port is not None and not self._is_port_in_use(preferred_port):
            return preferred_port
        return self._find_free_port()

    def _get_agent_profile(self, agent_id: str) -> str:
        return agent_id

    def _get_agent_gateway_port(self, agent_id: str) -> int | None:
        sandbox_state = self._repository.get_sandbox_state(agent_id)
        if sandbox_state is None:
            return None
        metadata = sandbox_state.sandbox_payload_json.get("metadata", {})
        port = metadata.get("gateway_port")
        return int(port) if isinstance(port, int) else None

    def list_agent_skills(self, agent_id: str) -> list[dict[str, Any]]:
        """查询当前 agent 对应 runtime 支持的 skills。"""
        builtin_skills = self._fetch_agent_skills_from_runtime(agent_id)
        try:
            self._sync_builtin_skills(agent_id, builtin_skills)
        except Exception:
            self._logger.warning(
                "Failed to sync builtin skills into DB: agent_id=%s",
                agent_id,
                exc_info=True,
            )
        self._logger.info(
            "Listed agent skills successfully: agent_id=%s skill_count=%s",
            agent_id,
            len(builtin_skills),
        )
        return builtin_skills

    def sync_installed_agent_skills(self, agent_id: str) -> list[dict[str, Any]]:
        """Fetch installed skills from runtime and replace DB records in one transaction."""
        builtin_skills = self._fetch_agent_skills_from_runtime(agent_id)
        self._repository.replace_installed_agent_skills_from_runtime(
            agent_id=agent_id,
            skills=builtin_skills,
        )
        self._logger.info(
            "Synced installed skills successfully: agent_id=%s skill_count=%s",
            agent_id,
            len(builtin_skills),
        )
        return builtin_skills

    def _fetch_agent_skills_from_runtime(self, agent_id: str) -> list[dict[str, Any]]:
        """Fetch current runtime-visible skills for one agent."""
        sandbox_state = self._get_sandbox_state(agent_id)
        self._logger.info(
            "Listing agent skills: agent_id=%s base_url=%s",
            agent_id,
            sandbox_state.adapter_base_url,
        )

        client: httpx.Client | None = None
        try:
            client = httpx.Client(base_url=sandbox_state.adapter_base_url, timeout=30.0)
            
            for attempt in range(10):
                try:
                    response = client.get("/agent/skills")
                    response.raise_for_status()
                    break
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 502 and attempt < 9:
                        self._logger.debug(
                            "Skills endpoint not ready yet (attempt %d/10): agent_id=%s",
                            attempt + 1,
                            agent_id,
                        )
                        time.sleep(1)
                        continue
                    raise
                except httpx.ConnectError as exc:
                    if attempt < 9:
                        self._logger.debug(
                            "Skills endpoint connection failed (attempt %d/10): agent_id=%s",
                            attempt + 1,
                            agent_id,
                        )
                        time.sleep(1)
                        continue
                    raise
            
            payload = response.json()

            if isinstance(payload, list):
                skills = payload
            else:
                skills = payload.get("skills", []) if isinstance(payload, dict) else []

            if not isinstance(skills, list):
                self._logger.warning(
                    "Agent skills response has invalid format: agent_id=%s payload_type=%s",
                    agent_id,
                    type(payload).__name__,
                )
                return []

            return [item for item in skills if isinstance(item, dict)]
        except Exception:
            self._logger.exception("Failed to list agent skills: agent_id=%s", agent_id)
            raise
        finally:
            if client is not None:
                client.close()

    def _sync_builtin_skills(self, agent_id: str, skills: list[dict[str, Any]]) -> None:
        for item in skills:
            skill_name = item.get("name")
            if not isinstance(skill_name, str) or not skill_name.strip():
                continue

            normalized_name = skill_name.strip()
            skill_source = item.get("source")
            source_value = skill_source if isinstance(skill_source, str) else None
            file_path = item.get("filePath")
            relative_path = file_path if isinstance(file_path, str) else None
            skill_id = self._build_builtin_skill_id(agent_id, normalized_name)

            self._repository.upsert_builtin_skill(
                skill_id=skill_id,
                skill_name=normalized_name,
                metadata=dict(item),
                skill_source=source_value,
                relative_path=relative_path,
            )
            self._repository.upsert_installed_agent_skill(
                agent_id=agent_id,
                skill_id=skill_id,
                source_type='builtin',
                repo_id=None,
                skill_name=normalized_name,
                relative_path=relative_path,
                metadata=dict(item),
                skill_source=source_value,
            )

    def _build_builtin_skill_id(self, agent_id: str, skill_name: str) -> str:
        return str(uuid5(NAMESPACE_URL, f"builtin:{agent_id}:{skill_name}"))

    async def install_agent_skill(self, agent_id: str, skill_name: str, source_path: str | None = None) -> dict[str, Any]:
        """下发 skill 到 runtime。"""
        agent = self._get_agent(agent_id)

        if agent.status is AgentStatus.paused:
            agent = await self.resume_agent(agent_id)
        elif agent.status is not AgentStatus.running:
            raise DomainError(
                code=AGENT_NOT_RUNNING,
                message="Agent must be running to install skills.",
                details={"agent_id": agent_id, "status": agent.status.value},
            )

        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            try:
                request_body: dict[str, Any] = {"skill_name": skill_name}
                if source_path:
                    request_body["source_path"] = source_path
                payload = await adaptor_client.post(
                    f"/agent/skills/install?id={agent_id}",
                    json=request_body,
                )
            except httpx.HTTPError as exc:
                raise DomainError(
                    code=AGENT_SKILL_INSTALL_FAILED,
                    message="Failed to install skill on runtime.",
                    details={
                        "agent_id": agent_id,
                        "skill_name": skill_name,
                        "error": str(exc),
                    },
                ) from exc
        finally:
            await adaptor_client.close()

        if not isinstance(payload, dict):
            return {"status": "accepted"}
        return payload

    async def uninstall_agent_skill(self, agent_id: str, skill_name: str, source_type: str | None = None, source_path: str | None = None) -> dict[str, Any]:
        """从 runtime 卸载 skill。"""
        agent = self._get_agent(agent_id)

        if agent.status is AgentStatus.paused:
            agent = await self.resume_agent(agent_id)
        elif agent.status is not AgentStatus.running:
            raise DomainError(
                code=AGENT_NOT_RUNNING,
                message="Agent must be running to uninstall skills.",
                details={"agent_id": agent_id, "status": agent.status.value},
            )

        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            try:
                request_body: dict[str, Any] = {"skill_name": skill_name}
                if source_type:
                    request_body["source_type"] = source_type
                if source_path:
                    request_body["source_path"] = source_path
                payload = await adaptor_client.post(
                    f"/agent/skills/uninstall?id={agent_id}",
                    json=request_body,
                )
            except httpx.HTTPError as exc:
                raise DomainError(
                    code=AGENT_SKILL_UNINSTALL_FAILED,
                    message="Failed to uninstall skill on runtime.",
                    details={
                        "agent_id": agent_id,
                        "skill_name": skill_name,
                        "error": str(exc),
                    },
                ) from exc
        finally:
            await adaptor_client.close()

        if not isinstance(payload, dict):
            return {"status": "accepted"}
        return payload

    def create_agent(self, request: AgentCreateRequest) -> AgentCreateResult:
        agent_id = str(uuid4())
        prefix = _log_prefix(agent_id=agent_id)
        logger.info(f"{prefix}Creating agent: name=%s sandbox_type=%s", request.name, request.sandbox_type)
        
        profile_name = agent_id
        gateway_port = self._find_free_port()
        logger.info(f"{prefix}Using profile: {profile_name}, gateway_port: {gateway_port}")
        
        workspace_path = str(self._workspace_store.init_workspace(agent_id))
        logger.info(f"{prefix}Workspace initialized: path=%s", workspace_path)
        sandbox_handle: SandboxHandle | None = None
        try:
            self._create_agent_record(
                agent_id=agent_id,
                request=request,
                workspace_path=workspace_path,
            )
            logger.info(f"{prefix}Agent record created, starting sandbox...")
            sandbox_handle = self._sandbox_backend.start(
                agent_id=agent_id,
                workspace_path=workspace_path,
                profile=profile_name,
                gateway_port=gateway_port,
            )
            logger.info(f"{prefix}Sandbox started: sandbox_id=%s", sandbox_handle.sandbox_id)
            adapter_endpoint = self._sandbox_backend.endpoint(sandbox_handle)
            logger.info(f"{prefix}Adapter endpoint ready: url=%s", adapter_endpoint.base_url)
            sandbox_payload = self._sandbox_handle_payload(sandbox_handle)
            sandbox_payload["metadata"]["gateway_port"] = gateway_port
            self._repository.save_sandbox_state(
                agent_id,
                sandbox_payload_json=sandbox_payload,
                adapter_base_url=adapter_endpoint.base_url,
                adapter_ready=True,
            )
            logger.info(f"{prefix}Sandbox state saved to database")

            logger.info(f"{prefix}Waiting for sandbox health check...")
            client: httpx.Client | None = None
            for i in range(30):
                try:
                    client = httpx.Client(base_url=adapter_endpoint.base_url, timeout=5.0)
                    response = client.get("/ping")
                    if response.status_code == 200:
                        logger.info(f"{prefix}Sandbox ready after %d attempts", i + 1)
                        break
                except Exception as exc:
                    logger.debug(f"{prefix}Health check attempt %d failed: %s", i + 1, exc)
                    pass
                finally:
                    if client is not None:
                        client.close()
                time.sleep(1)
            else:
                logger.error(f"{prefix}Sandbox health check timeout after 30 attempts")
                raise DomainError(
                    code=AGENT_CREATE_FAILED,
                    message="Sandbox health check timeout.",
                    details={"agent_id": agent_id},
                )

            logger.info(f"{prefix}Calling /agent/start...")
            client = httpx.Client(base_url=adapter_endpoint.base_url, timeout=120.0)
            try:
                start_payload = self._build_agent_start_payload(request, profile_name, gateway_port)
                logger.debug(f"{prefix}/agent/start payload: %s", start_payload)
                start_response = client.post("/agent/start", json=start_payload)
                start_response.raise_for_status()
                logger.info(f"{prefix}/agent/start succeeded: status=%d", start_response.status_code)
                started_agent = start_response.json()
                remote_runtime_agent_id = started_agent.get("id")
                if not isinstance(remote_runtime_agent_id, str) or not remote_runtime_agent_id:
                    raise DomainError(
                        code=AGENT_CREATE_FAILED,
                        message="Started agent response missing runtime agent id.",
                        details={"agent_id": agent_id},
                    )
            except httpx.HTTPStatusError as exc:
                logger.error(f"{prefix}/agent/start failed: %s", exc)
                raise DomainError(
                    code=AGENT_CREATE_FAILED,
                    message="Failed to start agent.",
                    details={"agent_id": agent_id, "error": str(exc)},
                ) from exc
            finally:
                client.close()

            logger.info(f"{prefix}Calling /agent/sessions...")
            client = httpx.Client(base_url=adapter_endpoint.base_url, timeout=30.0)
            try:
                response = client.post(f"/agents/{remote_runtime_agent_id}/sessions", json={})
                response.raise_for_status()
                logger.info(f"{prefix}/agent/sessions succeeded: status=%d", response.status_code)
                session_data = response.json()
                logger.debug(f"{prefix}Session data: %s", session_data)
            except httpx.HTTPStatusError as exc:
                logger.error(f"{prefix}/agent/sessions failed: %s", exc)
                raise DomainError(
                    code=AGENT_CREATE_FAILED,
                    message="Failed to create session on agent.",
                    details={"agent_id": agent_id, "error": str(exc)},
                ) from exc
            finally:
                client.close()

            running_agent = self._repository.update_agent_status(
                agent_id,
                AgentStatus.running,
            )
            logger.info(f"{prefix}Agent creation complete: status=running")

            # 同步 builtin skills 到数据库
            try:
                self.sync_installed_agent_skills(agent_id)
                logger.info(f"{prefix}Builtin skills synced successfully")
            except Exception:
                logger.warning(f"{prefix}Failed to sync builtin skills, continuing...", exc_info=True)

            return AgentCreateResult(
                agent=replace(running_agent, workspace_path=workspace_path),
            )
        except Exception as exc:
            logger.error(f"{prefix}Agent creation failed: error=%s", exc, exc_info=True)
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
            self._raise_operation_failed(
                code=AGENT_CREATE_FAILED,
                message="Agent creation failed.",
                agent_id=agent_id,
                cause=exc,
                cleanup_errors=cleanup_errors,
            )

    def _build_agent_start_payload(
        self,
        request: AgentCreateRequest,
        profile: str,
        gateway_port: int,
    ) -> dict[str, Any]:
        """构建 /agent/start 请求的 payload。"""
        model = self._repository.get_model(request.model_id)
        if model is not None:
            model_info = {
                "name": model.name,
                "provider": model.provider,
                "api_key": model.api_key,
                "api_base_url": model.api_base_url,
                "compatibility": model.compatibility,
            }
        else:
            model_info = {}

        return {
            "model_id": request.model_id,
            "model": model_info,
            "profile": profile,
            "gateway_port": gateway_port,
        }

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
        """从 deleted 状态恢复（备份功能暂禁用）"""
        agent = self._get_agent(agent_id)

        # 1. 检查备份（暂注释，备份功能已禁用）
        # backup_store = RuntimeBackupStore()
        # if not backup_store.backup_exists(agent_id, agent.adapter_type):
        #     raise DomainError(
        #         code=RUNTIME_BACKUP_NOT_FOUND,
        #         message="Runtime backup not found.",
        #         details={"agent_id": agent_id},
        #     )
        # backup_store.restore(agent_id, agent.adapter_type)

        # 2. 重新启动沙箱（不依赖备份）
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

        # 6. 调用 /agent/start（使用完整的恢复 payload）
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            try:
                start_payload = self._build_agent_start_payload_for_recovery(agent)
                await adaptor_client.post("/agent/start", json=start_payload)
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
        prefix = _log_prefix(agent_id=agent_id)
        logger.info(f"{prefix}Resuming agent from paused state")

        # 1. 验证沙箱是否仍在运行
        sandbox_state = self._get_sandbox_state(agent_id)

        # 2. 等待沙箱就绪（增加超时时间到60秒）
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            for attempt in range(60):
                if await adaptor_client.health_check():
                    logger.info(f"{prefix}Sandbox ready after {attempt + 1} attempts")
                    break
                await asyncio.sleep(1)
            else:
                raise DomainError(
                    code=SANDBOX_NOT_READY,
                    message="Sandbox health check timeout during resume from paused.",
                    details={"agent_id": agent_id},
                )
        finally:
            await adaptor_client.close()

        # 3. 调用 /agent/start（增加超时时间到180秒，因为可能需要加载模型）
        adaptor_client = self._get_adaptor_http_client(agent_id)
        remote_runtime_agent_id: str | None = None
        try:
            start_payload = self._build_agent_start_payload_for_recovery(agent)
            logger.debug(f"{prefix}/agent/start payload for resume from paused: %s", start_payload)
            try:
                started_agent = await adaptor_client.post("/agent/start", json=start_payload, timeout=180.0)
                remote_runtime_agent_id = started_agent.get("id")
                if not isinstance(remote_runtime_agent_id, str) or not remote_runtime_agent_id:
                    raise DomainError(
                        code=RUNTIME_START_FAILED,
                        message="Started agent response missing runtime agent id during resume from paused.",
                        details={"agent_id": agent_id},
                    )
                logger.info(f"{prefix}/agent/start succeeded: runtime_agent_id=%s", remote_runtime_agent_id)
            except httpx.HTTPStatusError as exc:
                logger.error(f"{prefix}/agent/start failed with HTTP error: %s", exc)
                raise DomainError(
                    code=RUNTIME_START_FAILED,
                    message="Failed to start runtime during resume from paused.",
                    details={"agent_id": agent_id, "error": str(exc)},
                ) from exc
            except httpx.ReadTimeout as exc:
                logger.error(f"{prefix}/agent/start timeout: %s", exc)
                raise DomainError(
                    code=RUNTIME_START_FAILED,
                    message="Runtime start timed out during resume from paused.",
                    details={"agent_id": agent_id, "error": "ReadTimeout"},
                ) from exc
            except httpx.ConnectError as exc:
                logger.error(f"{prefix}/agent/start connection failed: %s", exc)
                raise DomainError(
                    code=RUNTIME_START_FAILED,
                    message="Failed to connect to runtime during resume from paused.",
                    details={"agent_id": agent_id, "error": "ConnectError"},
                ) from exc
        finally:
            await adaptor_client.close()

        # 4. 创建 session（与 create_agent 保持一致）
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            try:
                response = await adaptor_client.post(
                    f"/agents/{remote_runtime_agent_id}/sessions",
                    json={},
                    timeout=30.0,
                )
                logger.info(f"{prefix}/agents/sessions succeeded: session_id=%s", response.get("id", "unknown"))
            except httpx.HTTPStatusError as exc:
                logger.error(f"{prefix}/agents/sessions failed: %s", exc)
                raise DomainError(
                    code=AGENT_CREATE_FAILED,
                    message="Failed to create session on agent during resume from paused.",
                    details={"agent_id": agent_id, "error": str(exc)},
                ) from exc
        finally:
            await adaptor_client.close()

        # 5. 更新状态
        return self._repository.update_agent_status(agent_id, AgentStatus.running)

    async def resume_agent(self, agent_id: str) -> AgentRecord:
        agent = self._get_agent(agent_id)

        if agent.status == AgentStatus.paused:
            self._ensure_transition(agent, AgentStatus.running)
            return await self._resume_from_paused(agent_id)
        elif agent.status == AgentStatus.deleted:
            return await self._resume_from_deleted(agent_id)
        elif agent.status == AgentStatus.running:
            return await self._resume_from_running(agent_id)
        else:
            raise DomainError(
                code=INVALID_AGENT_TRANSITION,
                message="Cannot resume from current status.",
                details={"agent_id": agent_id, "status": agent.status.value},
            )

    async def _resume_from_running(self, agent_id: str) -> AgentRecord:
        """从 running 状态恢复（用于服务重启时，子进程已停止但数据库状态仍为 running）"""
        agent = self._get_agent(agent_id)
        prefix = _log_prefix(agent_id=agent_id)
        logger.info(f"{prefix}Resuming agent from running state (service restart recovery)")

        # 获取隔离参数
        profile_name = agent_id
        saved_gateway_port = self._get_agent_gateway_port(agent_id)
        
        # 检查端口是否被占用，如果被占用则分配新端口
        gateway_port = self._get_available_gateway_port(saved_gateway_port)
        if saved_gateway_port is not None and gateway_port != saved_gateway_port:
            logger.warning(f"{prefix}Saved gateway port {saved_gateway_port} is in use, using new port {gateway_port}")

        # 1. 重新启动沙箱（传递隔离参数）
        sandbox_handle = self._sandbox_backend.start(
            agent_id=agent_id,
            workspace_path=agent.workspace_path,
            profile=profile_name,
            gateway_port=gateway_port,
        )
        adapter_endpoint = self._sandbox_backend.endpoint(sandbox_handle)
        logger.info(f"{prefix}Sandbox restarted: base_url=%s profile=%s gateway_port=%s",
                    adapter_endpoint.base_url, profile_name, gateway_port)

        # 2. 保存沙箱状态（包含 gateway_port）
        sandbox_payload = self._sandbox_handle_payload(sandbox_handle)
        sandbox_payload["metadata"]["gateway_port"] = gateway_port
        self._repository.save_sandbox_state(
            agent_id,
            sandbox_payload_json=sandbox_payload,
            adapter_base_url=adapter_endpoint.base_url,
            adapter_ready=True,
        )

        # 3. 等待沙箱就绪（增加超时时间到60秒）
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            for attempt in range(60):
                if await adaptor_client.health_check():
                    logger.info(f"{prefix}Sandbox ready after {attempt + 1} attempts")
                    break
                await asyncio.sleep(1)
            else:
                raise DomainError(
                    code=SANDBOX_NOT_READY,
                    message="Sandbox health check timeout during recovery.",
                    details={"agent_id": agent_id},
                )
        finally:
            await adaptor_client.close()

        # 4. 调用 /agent/start（增加超时时间到180秒，因为可能需要加载模型）
        adaptor_client = self._get_adaptor_http_client(agent_id)
        remote_runtime_agent_id: str | None = None
        try:
            start_payload = self._build_agent_start_payload_for_recovery(agent)
            logger.debug(f"{prefix}/agent/start payload for recovery: %s", start_payload)
            try:
                started_agent = await adaptor_client.post("/agent/start", json=start_payload, timeout=180.0)
                remote_runtime_agent_id = started_agent.get("id")
                if not isinstance(remote_runtime_agent_id, str) or not remote_runtime_agent_id:
                    raise DomainError(
                        code=RUNTIME_START_FAILED,
                        message="Started agent response missing runtime agent id during recovery.",
                        details={"agent_id": agent_id},
                    )
                logger.info(f"{prefix}/agent/start succeeded: runtime_agent_id=%s", remote_runtime_agent_id)
            except httpx.HTTPStatusError as exc:
                logger.error(f"{prefix}/agent/start failed with HTTP error: %s", exc)
                raise DomainError(
                    code=RUNTIME_START_FAILED,
                    message="Failed to start runtime during recovery.",
                    details={"agent_id": agent_id, "error": str(exc)},
                ) from exc
            except httpx.ReadTimeout as exc:
                logger.error(f"{prefix}/agent/start timeout: %s", exc)
                raise DomainError(
                    code=RUNTIME_START_FAILED,
                    message="Runtime start timed out during recovery.",
                    details={"agent_id": agent_id, "error": "ReadTimeout"},
                ) from exc
            except httpx.ConnectError as exc:
                logger.error(f"{prefix}/agent/start connection failed: %s", exc)
                raise DomainError(
                    code=RUNTIME_START_FAILED,
                    message="Failed to connect to runtime during recovery.",
                    details={"agent_id": agent_id, "error": "ConnectError"},
                ) from exc
        finally:
            await adaptor_client.close()

        # 5. 创建 session（与 create_agent 保持一致）
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            try:
                response = await adaptor_client.post(
                    f"/agents/{remote_runtime_agent_id}/sessions",
                    json={},
                    timeout=30.0,
                )
                logger.info(f"{prefix}/agents/sessions succeeded: session_id=%s", response.get("id", "unknown"))
            except httpx.HTTPStatusError as exc:
                logger.error(f"{prefix}/agents/sessions failed: %s", exc)
                raise DomainError(
                    code=AGENT_CREATE_FAILED,
                    message="Failed to create session on agent during recovery.",
                    details={"agent_id": agent_id, "error": str(exc)},
                ) from exc
        finally:
            await adaptor_client.close()

        # 6. 更新状态（保持 running 状态，但更新时间戳）
        return self._repository.update_agent_status(agent_id, AgentStatus.running)

    def _build_agent_start_payload_for_recovery(self, agent: AgentRecord) -> dict[str, Any]:
        """构建恢复时 /agent/start 请求的 payload"""
        if agent.model_id is None:
            model_info = {}
        else:
            model = self._repository.get_model(agent.model_id)
            if model is not None:
                model_info = {
                    "name": model.name,
                    "provider": model.provider,
                    "api_key": model.api_key,
                    "api_base_url": model.api_base_url,
                    "compatibility": model.compatibility,
                }
            else:
                model_info = {}

        profile_name = agent.id
        sandbox_state = self._repository.get_sandbox_state(agent.id)
        
        # 从 sandbox_payload_json["metadata"] 中获取 gateway_port
        gateway_port = None
        if sandbox_state is not None:
            metadata = sandbox_state.sandbox_payload_json.get("metadata", {})
            port_val = metadata.get("gateway_port")
            if isinstance(port_val, int):
                gateway_port = port_val
        
        if gateway_port is None:
            gateway_port = self._find_free_port()

        return {
            "model_id": agent.model_id,
            "model": model_info,
            "profile": profile_name,
            "gateway_port": gateway_port,
        }

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
        self._auto_generate_session_title(agent_id, session_id)

        ws_content = self._maybe_prepend_interruption_prefix(session_id, content)
        ws_client = await self._prepare_ws_message_client(agent_id, session_id, ws_content)

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
                self._sync_runtime_session_identity_from_event(
                    agent_id=agent_id,
                    session_id=session_id,
                    event=event_dict,
                )
                # 刷新session状态
                self._sync_session_state_from_event(
                    agent_id=agent_id,
                    session_id=session_id,
                    event=event_dict,
                )

                # Handle client.error events from witty-agent-server
                if event_dict["type"] in {"client.error", "stream.error"}:
                    error_payload = event_dict.get("payload", {})
                    error_code = error_payload.get("code", "UNKNOWN_ERROR")
                    error_message = error_payload.get("message", "Unknown error from adaptor")
                    raise DomainError(
                        code=error_code,
                        message=error_message,
                        details={"session_id": session_id, "agent_id": agent_id},
                    )
                if self._should_filter_session_event(event_dict):
                    self._logger.info(
                        "filtered session state event from response: agent_id=%s session_id=%s event_type=%s",
                        agent_id,
                        session_id,
                        event_dict["type"],
                    )
                    continue
                self._logger.info(f"received event: {json.dumps(event_dict, indent=2, ensure_ascii=False)}")
                events.append(event_dict)
                if event_dict["type"] in {"message.completed", "turn.completed"}:
                    has_completed = True
                    self._logger.info("message.completed received, stopping")
                    break
        except Exception:
            self._session_manager.upsert_session(
                session_id=session_id,
                agent_id=agent_id,
                status="error",
            )
            raise
        finally:
            await self._close_ws_message_client(
                agent_id=agent_id,
                session_id=session_id,
                ws_client=ws_client,
            )
            await client_closer()

        if not has_completed:
            raise DomainError(
                code="INVALID_MESSAGE_STREAM",
                message="Message stream terminated before completion event.",
                details={
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "events_count": len(events),
                    "last_event_type": events[-1].get("type") if events else None,
                },
            )
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
        self._auto_generate_session_title(agent_id, session_id)

        ws_content = self._maybe_prepend_interruption_prefix(session_id, content)
        ws_client = await self._prepare_ws_message_client(agent_id, session_id, ws_content)

        # Start a new stream generation
        stream_gen = _stream_registry.start_stream(session_id)
        queue = _stream_registry.subscribe(session_id)

        seq_no = 0
        assistant_text = ""
        assistant_msg_id: str | None = None
        terminal_received = False
        tokens_since_checkpoint = 0
        last_checkpoint_time = time.monotonic()
        TOKENS_PER_CHECKPOINT = 100
        CHECKPOINT_INTERVAL_S = 2.0
        sandbox_type = agent.sandbox_type

        async def consume_ws() -> None:
            """后台任务：消费 WebSocket 事件，持久化，并推送到stream_registry"""
            nonlocal seq_no, assistant_text, assistant_msg_id, terminal_received
            nonlocal tokens_since_checkpoint, last_checkpoint_time

            try:
                async for event in ws_client.recv():
                    event_dict = dict(event)
                    self._sync_runtime_session_identity_from_event(
                        agent_id=agent_id,
                        session_id=session_id,
                        event=event_dict,
                    )
                    self._sync_session_state_from_event(
                        agent_id=agent_id,
                        session_id=session_id,
                        event=event_dict,
                    )

                    if event_dict["type"] in {"client.error", "stream.error"}:
                        error_payload = event_dict.get("payload", {})
                        error_code = error_payload.get("code", "UNKNOWN_ERROR")
                        error_message = error_payload.get("message", "Unknown error from adaptor")
                        self._logger.error(
                            "Stream error in background consumer: agent_id=%s session_id=%s code=%s message=%s",
                            agent_id, session_id, error_code, error_message,
                        )
                        _stream_registry.end_stream(session_id)
                        return

                    if self._should_filter_session_event(event_dict):
                        self._logger.info(
                            "filtered session state event from stream: agent_id=%s session_id=%s event_type=%s",
                            agent_id, session_id, event_dict["type"],
                        )
                        continue

                    if assistant_msg_id is None:
                        assistant_msg_id = self._repository.create_message(
                            agent_id=agent_id,
                            session_id=session_id,
                            role="assistant",
                            content="",
                            status=MessageStatus.generating,
                        )

                    seq_no += 1
                    event_type = event_dict["type"]
                    payload = event_dict.get("payload") if isinstance(event_dict.get("payload"), dict) else {}
                    try:
                        self._repository.create_message_event_with_retry(
                            agent_id=agent_id,
                            session_id=session_id,
                            event_type=event_type,
                            payload_json=payload,
                            seq_no=seq_no,
                            message_id=assistant_msg_id,
                        )
                    except Exception:
                        self._logger.warning(
                            "Failed to persist event: agent_id=%s session_id=%s event_type=%s",
                            agent_id, session_id, event_type, exc_info=True,
                        )

                    if event_type == "message.delta":
                        delta = payload.get("delta", "")
                        assistant_text += delta
                        tokens_since_checkpoint += len(delta) // 4
                    elif event_type == "message.completed":
                        completed_text = payload.get("text", "")
                        if completed_text:
                            assistant_text = completed_text

                    now = time.monotonic()
                    if assistant_msg_id and (
                        tokens_since_checkpoint >= TOKENS_PER_CHECKPOINT
                        or now - last_checkpoint_time >= CHECKPOINT_INTERVAL_S
                        or event_type in {"message.completed", "turn.completed"}
                    ):
                        if assistant_text:
                            try:
                                self._repository.update_message_content(assistant_msg_id, assistant_text)
                                self._repository.update_message_stream_at(assistant_msg_id)
                            except Exception:
                                self._logger.warning(
                                    "Failed to update message content checkpoint: msg_id=%s",
                                    assistant_msg_id, exc_info=True,
                                )
                        tokens_since_checkpoint = 0
                        last_checkpoint_time = now

                    if event_type in {"message.completed", "turn.completed"}:
                        terminal_received = True
                        if assistant_msg_id:
                            try:
                                self._repository.update_message_status(assistant_msg_id, MessageStatus.completed)
                                self._logger.info(
                                "update_message_status in ws: assistant_msg_id=%s state=%s",
                                assistant_msg_id,
                                MessageStatus.completed,
                                )
                            except Exception:
                                self._logger.warning(
                                    "Failed to update message status: msg_id=%s",
                                    assistant_msg_id, exc_info=True,
                                )
                            try:
                                self._repository.compact_message_delta_events(assistant_msg_id)
                            except Exception:
                                self._logger.warning(
                                    "Failed to compact delta events: msg_id=%s",
                                    assistant_msg_id, exc_info=True,
                                )

                    _stream_registry.push_event(session_id, event_dict, stream_gen)

                    if event_type in {"message.completed", "turn.completed"}:
                        break
            except Exception:
                self._logger.warning(
                    "Background WS consumer error: agent_id=%s session_id=%s",
                    agent_id, session_id, exc_info=True,
                )
            finally:
                _stream_registry.end_stream(session_id)
                _stream_registry.cleanup(session_id)
                await self._close_ws_message_client(
                    agent_id=agent_id,
                    session_id=session_id,
                    ws_client=ws_client,
                )

        bg_task = asyncio.create_task(consume_ws())

        try:
            while True:
                event_dict = await queue.get()
                if event_dict is None:  # sentinel — stream ended
                    break
                yield {
                    "sandbox_type": sandbox_type,
                    "event": event_dict,
                }
        except GeneratorExit:
            self._logger.info(
                "SSE client disconnected: agent_id=%s session_id=%s — background consumer continues",
                agent_id, session_id,
            )
            _stream_registry.unsubscribe(session_id, queue)
            raise
        except asyncio.CancelledError:
            self._logger.info(
                "SSE stream cancelled: agent_id=%s session_id=%s — background consumer continues",
                agent_id, session_id,
            )
            _stream_registry.unsubscribe(session_id, queue)
            raise
        except Exception:
            self._logger.exception(
                "SSE stream error: agent_id=%s session_id=%s",
                agent_id, session_id,
            )
            _stream_registry.unsubscribe(session_id, queue)
            raise

    async def reconnect_stream(
        self,
        agent_id: str,
        session_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        agent = self._get_agent(agent_id)
        sandbox_type = agent.sandbox_type

        if not _stream_registry.is_active(session_id):
            self._logger.info(
                "reconnect_stream: no active stream for session_id=%s", session_id,
            )
            return

        # Replay buffered events
        buffered = _stream_registry.get_buffered_events(session_id)
        self._logger.info(
            "reconnect_stream: replaying %d buffered events for session_id=%s",
            len(buffered), session_id,
        )
        for event_dict in buffered:
            yield {
                "sandbox_type": sandbox_type,
                "event": event_dict,
            }

        # Subscribe to live events
        queue = _stream_registry.subscribe(session_id)
        try:
            while True:
                event_dict = await queue.get()
                if event_dict is None:
                    break
                yield {
                    "sandbox_type": sandbox_type,
                    "event": event_dict,
                }
        except GeneratorExit:
            _stream_registry.unsubscribe(session_id, queue)
            raise
        except asyncio.CancelledError:
            _stream_registry.unsubscribe(session_id, queue)
            raise
        except Exception:
            _stream_registry.unsubscribe(session_id, queue)
            raise

    async def _handle_user_abort(
        self,
        ws_client: WebSocketClient,
        agent_id: str,
        session_id: str,
    ) -> None:
        await ws_client.send({"type": "message.abort", "payload": {}})
        self._logger.info(
            "Sending message.abort via WS: agent_id=%s session_id=%s",
            agent_id,
            session_id,
        )

    async def create_session(
        self,
        agent_id: str,
        runtime_agent_id: str | None = None,
    ) -> SessionRecord:
        agent = self._get_agent(agent_id)
        if agent.status is not AgentStatus.running:
            raise DomainError(
                code=AGENT_NOT_RUNNING,
                message="Agent must be running to create session.",
                details={"agent_id": agent_id, "status": agent.status.value},
            )

        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            session = await self._session_manager.create_session_remote(
                agent_id,
                adaptor_client,
                runtime_agent_id=runtime_agent_id,
            )
        finally:
            await adaptor_client.close()

        return session

    async def list_sessions(
        self,
        agent_id: str,
        runtime_agent_id: str | None = None,
    ) -> list[SessionRecord]:
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            return await self._session_manager.list_sessions_remote(
                agent_id,
                adaptor_client,
                runtime_agent_id=runtime_agent_id,
            )
        finally:
            await adaptor_client.close()

    async def get_session(
        self,
        agent_id: str,
        session_id: str,
        runtime_agent_id: str | None = None,
    ) -> SessionRecord:
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            return await self._session_manager.get_session_remote(
                agent_id,
                session_id,
                adaptor_client,
                runtime_agent_id=runtime_agent_id,
            )
        finally:
            await adaptor_client.close()

    async def get_session_events(
        self,
        agent_id: str,
        session_id: str,
        offset: int = 0,
        limit: int = 50,
        runtime_agent_id: str | None = None,
    ) -> dict[str, Any]:
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            session = self._session_manager.get_session(agent_id, session_id)
            resolved_runtime_agent_id = session.remote_runtime_agent_id
            if resolved_runtime_agent_id is None:
                resolved_runtime_agent_id = await self._session_manager.resolve_runtime_agent_id(
                    adaptor_client=adaptor_client,
                    runtime_agent_id=runtime_agent_id,
                )
            return await adaptor_client.get(
                f"/agents/{resolved_runtime_agent_id}/sessions/{session_id}/events",
                params={"offset": offset, "limit": limit},
            )
        finally:
            await adaptor_client.close()

    async def delete_session(
        self,
        agent_id: str,
        session_id: str,
        runtime_agent_id: str | None = None,
    ) -> None:
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            await self._session_manager.delete_session_remote(
                agent_id,
                session_id,
                adaptor_client,
                runtime_agent_id=runtime_agent_id,
            )
        finally:
            await adaptor_client.close()

    async def abort_session(
        self,
        agent_id: str,
        session_id: str,
        runtime_agent_id: str | None = None,
    ) -> dict[str, object]:
        adaptor_client = self._get_adaptor_http_client(agent_id)
        try:
            await self._session_manager.abort_session_remote(
                agent_id,
                session_id,
                adaptor_client,
                runtime_agent_id=runtime_agent_id,
            )
        finally:
            await adaptor_client.close()
        last_msg = self._repository.find_last_assistant_message_for_session(session_id)
        if last_msg is not None:
            try:
                self._repository.update_message_status(last_msg.id, MessageStatus.interrupted)
                self._logger.info(
                    "update_message_status in abort: msg_id=%s state=%s",
                    last_msg.id,
                    MessageStatus.interrupted,
                )
            except Exception:
                self._logger.warning(
                    "Failed to mark message interrupted after abort: msg_id=%s",
                    last_msg.id, exc_info=True,
                )

    async def delete_agent(self, agent_id: str) -> None:
        agent = self._get_agent(agent_id)
        sandbox_state = self._repository.get_sandbox_state(agent_id)

        cleanup_errors: list[dict[str, str]] = []

        # 1. 备份运行时（暂注释，避免占用空间）
        # if sandbox_state is not None:
        #     self._collect_error(
        #         cleanup_errors,
        #         "runtime_backup",
        #         lambda: self._backup_runtime(agent_id, agent.adapter_type),
        #     )

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
            # 彻底删除 agent 记录（包括关联的 session、message、skill 等），放在最后执行，确保前面步骤都完成了才删除记录
            self._repository.delete_agent(agent_id)
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
            model_id=request.model_id,
            mcp_server_list=request.mcp_server_list,
        )

    def _maybe_prepend_interruption_prefix(self, session_id: str, content: str) -> str:
        if self._repository.get_last_assistant_status(session_id) == "interrupted":
            self._logger.info("Last assistant message was interrupted, prepending interruption prefix: session_id=%s", session_id)
            return INTERRUPTION_PREFIX + content
        return content

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
        session = self._session_manager.get_session(agent_id, session_id)
        if session.remote_runtime_agent_id is None:
            raise DomainError(
                code="RUNTIME_AGENT_ID_MISSING",
                message="Remote runtime agent id was not found for session.",
                details={"agent_id": agent_id, "session_id": session_id},
            )
        base_url = sandbox_state.adapter_base_url
        if base_url.startswith("https"):
            scheme = "wss"
        elif base_url.startswith("http"):
            scheme = "ws"
        else:
            scheme = "ws"
        host = base_url.split("://")[-1]
        ws_base_url = f"{scheme}://{host}/agents/{session.remote_runtime_agent_id}"
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
            self._logger.info(f"_prepare_ws: connecting to {endpoint.base_url}/sessions/{session_id}/ws")
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

    async def _close_ws_message_client(
        self,
        *,
        agent_id: str,
        session_id: str,
        ws_client: WebSocketClient,
    ) -> None:
        """Close per-turn websocket so unread runtime events cannot leak into later turns."""
        close = getattr(ws_client, "close", None)
        if close is not None:
            try:
                await close()
            except Exception as exc:
                self._logger.warning(
                    "failed to close ws message client: agent_id=%s session_id=%s error=%s",
                    agent_id,
                    session_id,
                    exc,
                )
        self._ws_client_pool.remove_client(agent_id, session_id)

    def _sync_session_state_from_event(
        self,
        *,
        agent_id: str,
        session_id: str,
        event: dict[str, Any],
    ) -> None:
        """根据 adaptor WS 事件刷新本地 session 状态。"""
        event_type = event.get("type")
        payload = event.get("payload")
        normalized_payload = payload if isinstance(payload, dict) else {}

        if event_type in {"session.state_changed", "session.heartbeat"}:
            state = normalized_payload.get("state")
            if isinstance(state, str) and state in {"running", "idle", "error"}:
                self._logger.info(
                    "sync session state from ws event: agent_id=%s session_id=%s event_type=%s state=%s",
                    agent_id,
                    session_id,
                    event_type,
                    state,
                )
                self._session_manager.upsert_session(
                    session_id=session_id,
                    agent_id=agent_id,
                    status=state,
                )
            return

        if event_type in {"message.completed", "turn.completed"}:
            self._logger.info(
                "sync session state from ws event: agent_id=%s session_id=%s event_type=%s state=idle",
                agent_id,
                session_id,
                event_type,
            )
            self._session_manager.upsert_session(
                session_id=session_id,
                agent_id=agent_id,
                status="idle",
            )
            return

        if event_type in {"client.error", "stream.error"}:
            self._logger.info(
                "sync session state from ws event: agent_id=%s session_id=%s event_type=%s state=error",
                agent_id,
                session_id,
                event_type,
            )
            self._session_manager.upsert_session(
                session_id=session_id,
                agent_id=agent_id,
                status="error",
            )

    def _auto_generate_session_title(self, agent_id: str, session_id: str) -> None:
        """自动生成会话标题"""
        try:
            session = self._session_manager.get_session(agent_id, session_id)
            if session.title:
                return
            first_msg = self._repository.get_first_user_message(session_id)
            if first_msg:
                title = first_msg[:30].replace("\n", " ")
                self._repository.update_session_metadata(session_id, title=title)
        except Exception:
            self._logger.warning(
                "Failed to auto-generate session title: agent_id=%s session_id=%s",
                agent_id,
                session_id,
                exc_info=True,
            )

    def _sync_runtime_session_identity_from_event(
        self,
        *,
        agent_id: str,
        session_id: str,
        event: dict[str, Any],
    ) -> None:
        if event.get("type") != "session.runtime.changed":
            return

        payload = event.get("payload")
        normalized_payload = payload if isinstance(payload, dict) else {}
        runtime_session_id = normalized_payload.get("runtime_session_id")
        runtime_session_key = normalized_payload.get("runtime_session_key")
        runtime_type = event.get("runtime_type")

        if not isinstance(runtime_session_id, str) or not runtime_session_id:
            self._logger.warning(
                "skip runtime session identity sync due to missing runtime_session_id: "
                "agent_id=%s session_id=%s",
                agent_id,
                session_id,
            )
            return
        if not isinstance(runtime_session_key, str) or not runtime_session_key:
            self._logger.warning(
                "skip runtime session identity sync due to missing runtime_session_key: "
                "agent_id=%s session_id=%s",
                agent_id,
                session_id,
            )
            return
        if not isinstance(runtime_type, str) or not runtime_type:
            self._logger.warning(
                "skip runtime session identity sync due to missing runtime_type: "
                "agent_id=%s session_id=%s",
                agent_id,
                session_id,
            )
            return

        try:
            self._session_manager.get_session(agent_id, session_id)
            self._session_manager.update_session_runtime_identity(
                session_id=session_id,
                runtime_type=runtime_type,
                runtime_session_id=runtime_session_id,
                runtime_session_key=runtime_session_key,
            )
        except DomainError as exc:
            self._logger.exception(
                "failed to sync runtime session identity: "
                "agent_id=%s session_id=%s runtime_type=%s runtime_session_id=%s error_code=%s",
                agent_id,
                session_id,
                runtime_type,
                runtime_session_id,
                exc.code,
            )

    def _should_filter_session_event(self, event: dict[str, Any]) -> bool:
        """过滤仅用于本地 session 状态同步的内部事件。"""
        event_type = event.get("type")
        return event_type in {
            "session.state_changed",
            "session.heartbeat",
            "session.runtime.changed",
        }

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

        # 如果正在恢复中，跳过健康检查（避免恢复过程中状态被修改）
        if _recovery_lock.locked():
            self._logger.debug(
                "Skipping health check during recovery: agent_id=%s",
                agent_id,
            )
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
